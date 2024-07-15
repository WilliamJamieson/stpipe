import abc
import copy
import os.path
import tempfile
from collections.abc import Iterable, MutableMapping
from pathlib import Path
from types import MappingProxyType

import asdf

__all__ = ["LibraryError", "BorrowError", "ClosedLibraryError", "AbstractModelLibrary"]


class LibraryError(Exception):
    """
    Generic ModelLibrary related exception
    """


class BorrowError(LibraryError):
    """
    Exception indicating an issue with model borrowing
    """


class ClosedLibraryError(LibraryError):
    """
    Exception indicating a library method was used outside of a
    ``with`` context (that "opens" the library).
    """


class _Ledger(MutableMapping):
    """
    A "ledger" used for tracking checked out models.

    Each model has a unique "index" in the library which
    can be used to track the model. For ease-of-use this
    ledger maintains 2 mappings:

        - id (the id(model) result) to model index
        - index to model

    The "index to model" mapping keeps a reference to every
    model in the ledger (which allows id(model) to be consistent).

    The ledger is a MutableMapping that supports look up of:
        - index for a model
        - model for an index
    """

    def __init__(self):
        self._id_to_index = {}
        self._index_to_model = {}

    def __getitem__(self, model_or_index):
        if not isinstance(model_or_index, int):
            index = self._id_to_index[id(model_or_index)]
        else:
            index = model_or_index
        return self._index_to_model[index]

    def __setitem__(self, index, model):
        self._index_to_model[index] = model
        self._id_to_index[id(model)] = index

    def __delitem__(self, model_or_index):
        if isinstance(model_or_index, int):
            index = model_or_index
            model = self._index_to_model[index]
        else:
            model = model_or_index
            index = self._id_to_index[id(model)]
        del self._id_to_index[id(model)]
        del self._index_to_model[index]

    def __iter__(self):
        # only return indexes
        return iter(self._index_to_model)

    def __len__(self):
        return len(self._id_to_index)


class AbstractModelLibrary(abc.ABC):
    """
    A "library" of models (loaded from an association file).

    Do not anger the librarian!

    The library owns all models from the association and it will handle
    opening and closing files.

    Models can be "borrowed" from the library (by iterating through the
    library or "borrowing" a specific model). However the library must be
    "open" (used in a ``with`` context)  to borrow a model and the model
    must be "shelved" before the library "closes" (the ``with`` context exits).

    >>> with library:   # doctest: +SKIP
            model = library.borrow(0)  # borrow the first model
            # do stuff with the model
            library.shelve(model, 0)  # return the model

    Failing to "open" the library will result in a ClosedLibraryError.

    Failing to "return" a borrowed model will result in a BorrowError.
    """

    def __init__(
        self,
        init,
        asn_exptypes=None,
        asn_n_members=None,
        on_disk=False,
        temp_directory=None,
        **datamodels_open_kwargs,
    ):
        self._on_disk = on_disk
        self._open = False
        self._ledger = _Ledger()
        self._loaded_models = {}

        self._datamodels_open_kwargs = datamodels_open_kwargs

        if on_disk:
            if temp_directory is None:
                self._temp_dir = tempfile.TemporaryDirectory(dir="")
                self._temp_path = Path(self._temp_dir.name)
            else:
                self._temp_path = Path(temp_directory)
            self._temp_filenames = {}

        if asn_exptypes is not None:
            # if a single string, treat this as a single item list
            if isinstance(asn_exptypes, str):
                asn_exptypes = [asn_exptypes]
            # convert these to lower case to allow case insensitive matching below
            asn_exptypes = [exptype.lower() for exptype in asn_exptypes]

        if isinstance(init, (str, Path)):
            # init is an association filename (or path)
            asn_path = os.path.abspath(os.path.expanduser(os.path.expandvars(init)))
            self._asn_dir = os.path.dirname(asn_path)

            # load association
            asn_data = self._load_asn(asn_path)
        elif isinstance(init, MutableMapping):
            # we will modify the asn below so do a deep copy
            asn_data = copy.deepcopy(init)
            self._asn_dir = os.path.abspath(".")
        elif isinstance(init, self.__class__):
            raise ValueError(f"Invalid init {init}")
        elif isinstance(init, Iterable):  # assume a list of models
            if on_disk:
                raise ValueError("on_disk cannot be used for a list of models")

            # init is a list of models
            # make a fake asn from the models
            filenames = set()
            members = []
            for index, model_or_filename in enumerate(init):
                if asn_n_members is not None and len(members) == asn_n_members:
                    break
                if isinstance(model_or_filename, (str, Path)):
                    # TODO supporting a list of filenames by opening them as models
                    # has issues, if this is a widely supported mode (vs providing
                    # an association) it might make the most sense to make a fake
                    # association with the filenames at load time.
                    model = self._datamodels_open(
                        model_or_filename, **self._datamodels_open_kwargs
                    )
                else:
                    model = model_or_filename
                exptype = getattr(model.meta, "exptype", "SCIENCE")

                if asn_exptypes is not None and exptype.lower() not in asn_exptypes:
                    continue

                filename = model.meta.filename
                if filename in filenames:
                    raise ValueError(
                        f"Models in library cannot use the same filename: {filename}"
                    )
                filenames.add(filename)

                group_id = self._model_to_group_id(model)

                members.append(
                    {
                        "expname": filename,
                        "exptype": exptype,
                        "group_id": group_id,
                    }
                )

                self._loaded_models[len(self._loaded_models)] = model

            # since we've already filtered by asn type and n members reset these values
            asn_exptypes = None
            asn_n_members = None

            # make a fake association
            asn_data = {
                # TODO other asn information?
                "products": [
                    {
                        "members": members,
                    }
                ],
            }
            self._asn_dir = None
        else:
            raise ValueError(f"Invalid init {init}")

        if asn_exptypes is not None:
            asn_data["products"][0]["members"] = [
                m
                for m in asn_data["products"][0]["members"]
                if m["exptype"] in asn_exptypes
            ]

        if asn_n_members is not None:
            asn_data["products"][0]["members"] = asn_data["products"][0]["members"][
                :asn_n_members
            ]

        self._asn = asn_data
        self._members = self._asn["products"][0]["members"]

        for member in self._members:
            if "group_id" not in member:
                filename = os.path.join(self._asn_dir, member["expname"])
                member["group_id"] = self._filename_to_group_id(filename)

        if not on_disk:
            # if models were provided as input, assign the members here
            # now that the fake asn data is complete
            for i in self._loaded_models:
                self._assign_member_to_model(self._loaded_models[i], self._members[i])

    def __del__(self):
        if hasattr(self, "_temp_dir"):
            self._temp_dir.cleanup()

    @property
    def asn(self):
        # return a "read only" association
        def _to_read_only(obj):
            if isinstance(obj, dict):
                return MappingProxyType(obj)
            if isinstance(obj, list):
                return tuple(obj)
            return obj

        return asdf.treeutil.walk_and_modify(self._asn, _to_read_only)

    @property
    def group_names(self):
        names = set()
        for member in self._members:
            names.add(member["group_id"])
        return names

    @property
    def group_indices(self):
        group_dict = {}
        for i, member in enumerate(self._members):
            group_id = member["group_id"]
            if group_id not in group_dict:
                group_dict[group_id] = []
            group_dict[group_id].append(i)
        return group_dict

    def __len__(self):
        return len(self._members)

    def borrow(self, index):
        if not self._open:
            raise ClosedLibraryError("ModelLibrary is not open")

        # if model was already borrowed, raise
        if index in self._ledger:
            raise BorrowError("Attempt to double-borrow model")

        # if this model is in memory, return it
        if self._on_disk:
            if index in self._temp_filenames:
                model = self._datamodels_open(
                    self._temp_filenames[index], **self._datamodels_open_kwargs
                )
            else:
                model = self._load_member(index)
        else:
            if index in self._loaded_models:
                model = self._loaded_models[index]
            else:
                model = self._load_member(index)
                self._loaded_models[index] = model

        self._ledger[index] = model
        return model

    def _temp_path_for_model(self, model, index):
        model_filename = self._model_to_filename(model)
        subpath = self._temp_path / f"{index}"
        os.makedirs(subpath)
        return subpath / model_filename

    def shelve(self, model, index=None, modify=True):
        if not self._open:
            raise ClosedLibraryError("ModelLibrary is not open")

        if index is None:
            index = self._ledger[model]

        if index not in self._ledger:
            raise BorrowError("Attempt to shelve non-borrowed model")

        if modify:
            if self._on_disk:
                if index in self._temp_filenames:
                    temp_filename = self._temp_filenames[index]
                else:
                    temp_filename = self._temp_path_for_model(model, index)
                    self._temp_filenames[index] = temp_filename
                model.save(temp_filename)
            else:
                self._loaded_models[index] = model

        del self._ledger[index]

    def __iter__(self):
        for i in range(len(self)):
            yield self.borrow(i)

    def _assign_member_to_model(self, model, member):
        for attr in ("group_id", "tweakreg_catalog", "exptype"):
            if attr in member:
                setattr(model.meta, attr, member[attr])

        if not hasattr(model.meta, "asn"):
            model.meta.asn = {}

        model.meta.asn["table_name"] = self.asn.get("table_name", "")
        model.meta.asn["pool_name"] = self.asn.get("asn_pool", "")

    def _load_member(self, index):
        member = self._members[index]
        filename = os.path.join(self._asn_dir, member["expname"])

        model = self._datamodels_open(filename, **self._datamodels_open_kwargs)

        self._assign_member_to_model(model, member)

        return model

    def __copy__(self):
        # TODO make copy and deepcopy distinct and not require loading
        # all models into memory
        if self._on_disk:
            raise Exception()
        with self:
            model_copies = []
            for i, model in enumerate(self):
                model_copies.append(model.copy())
                self.shelve(model, i, modify=False)
        return self.__class__(model_copies)

    def __deepcopy__(self, memo):
        return self.__copy__()

    def copy(self, memo=None):
        return copy.deepcopy(self, memo=memo)

    def save(self, path=None, dir_path=None, save_model_func=None, overwrite=True):
        # FIXME: the signature for this function can lead to many possible outcomes
        # stpipe may call this with save_model_func and path defined
        # skymatch tests call with just dir_path
        # stpipe sometimes provides overwrite=True

        if path is None:

            def path(file_path, index):
                return file_path

        elif not callable(path):

            def path(file_path, index):
                path_head, path_tail = os.path.split(file_path)
                base, ext = os.path.splitext(path_tail)
                if index is not None:
                    base = base + str(index)
                return os.path.join(path_head, base + ext)

        # FIXME: since path is the first argument this means that calling
        # ModelLibrary.save("my_directory") will result in saving all models
        # to the current directory, ignoring "my_directory" this matches
        # what was done for ModelContainer
        dir_path = dir_path if dir_path is not None else os.getcwd()

        output_paths = []
        with self:
            for i, model in enumerate(self):
                if len(self) == 1:
                    index = None
                else:
                    index = i
                if save_model_func is None:
                    filename = model.meta.filename
                    output_path, output_filename = os.path.split(path(filename, index))

                    # use dir_path when provided
                    output_path = output_path if dir_path is None else dir_path

                    # create final destination (path + filename)
                    save_path = os.path.join(output_path, output_filename)

                    model.save(save_path)  # TODO save args?

                    output_paths.append(save_path)
                else:
                    output_paths.append(save_model_func(model, idx=index))

                self.shelve(model, i, modify=False)

        return output_paths

    def get_crds_parameters(self):
        """
        Get the "crds_parameters" from either:
            - the first "science" member (based on model.meta.exptype)
            - the first model (if no "science" member is found)
        """
        with self:
            science_index = None
            for i, member in self.members:
                if member["exptype"].lower() == "science":
                    science_index = i
                    break
            if science_index is None:
                # TODO warn if we did not find a science member
                science_index = 0
            model = self.borrow(science_index)
            parameters = model.get_crds_parameters()
            self.shelve(model, science_index, modify=False)
        return parameters

    def finalize_result(self, step, reference_files_used):
        with self:
            for i, model in enumerate(self):
                step.finalize_result(model, reference_files_used)
                self.shelve(model, i)

    def __enter__(self):
        self._open = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._open = False
        if exc_value:
            # if there is already an exception, don't worry about checking the ledger
            # instead allowing the calling code to raise the original error to provide
            # a more useful feedback without any chained ledger exception about
            # un-returned models
            return
        if self._ledger:
            raise BorrowError(
                f"ModelLibrary has {len(self._ledger)} un-returned models"
            )

    def map_function(self, function, modify=False):
        with self:
            for i, model in enumerate(self):
                try:
                    yield function(model)
                finally:
                    # this is in a finally to allow cleanup if the generator is
                    # deleted after it finishes (when it's not fully consumed)
                    self.shelve(model, i, modify)

    def _model_to_filename(self, model):
        model_filename = model.meta.filename
        # TODO patched by sub-class?
        if model_filename is None:
            model_filename = "model.asdf"
        return model_filename

    @property
    @abc.abstractmethod
    def crds_observatory(self):
        """
        Return the name of the observatory as a string
        """

    @abc.abstractmethod
    def _datamodels_open(self, filename, **kwargs):
        """
        Open a model from a filename
        """

    @classmethod
    @abc.abstractmethod
    def _load_asn(cls, filename):
        """
        Load an association from a filename
        """

    @abc.abstractmethod
    def _filename_to_group_id(self, filename):
        """
        Compute a "group_id" without loading the file as a DataModel

        This function will return the meta.group_id stored in the ASDF
        extension (if it exists) or a group_id calculated from the
        FITS headers.
        """
        # asdf_yaml = asdf.util.load_yaml(filename)
        # if group_id := asdf_yaml["roman"]["meta"].get("group_id"):
        #     return group_id
        # return _mapping_to_group_id(asdf_yaml["roman"]["meta"]["observation"])

    @abc.abstractmethod
    def _model_to_group_id(self, model):
        """
        Compute a "group_id" from a model using the DataModel interface
        """
        # if (group_id := getattr(model.meta, "group_id", None)) is not None:
        #     return group_id
        # return _mapping_to_group_id(model.meta.observation)
