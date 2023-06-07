from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.utils import IntegrityError

from abc import ABC, abstractmethod
import datetime
import json
import os.path
import re
import zipfile
import magic
from typing import List

from packaging import version

from .utils import MetaDBError
from .models import ContainerType, DataSet, DataSetBase, File, Keyword,\
                    Software
from .test_utils import parse_test_data


def _datetime_parser(datetime_str: str) -> datetime.datetime:
    """
    Convert an ISO 8601 timestamp string to a python datetime object.

    :param datetime_str: Timestamp string.

    :return: Timestamp as datetime.datetime object.
    """
    try:
        dt = datetime.datetime.fromisoformat(datetime_str)
        return dt
    except ValueError:
        try:
            dt = datetime.datetime.strptime(datetime_str,
                                            "%Y-%m-%dT%H:%M:%S+%z")
            return dt.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            dt = datetime.datetime.strptime(datetime_str,
                                            "%Y-%m-%d %H:%M:%S %Z")
            return dt.replace(tzinfo=datetime.timezone.utc)


def _used_software_parser(used_software_list: List[dict]) -> List[Software]:
    """
    Convert a list of dictionaries of Softwares to a list of Software objects.

    :param used_software_list: List of dictionaries of Softwares, as found in
    "content.json"

    :return: List of Software objects.
    """
    return [Software.to_Software(entry) for entry in used_software_list]


def _replaces_parser(replaces: str) -> DataSet:
    """
    Return a DataSet object for a given UUID string.

    :param replaces: UUID str of the replaced DataSet.

    :return: DataSet object of the replaced DataSet.
    """
    try:
        return DataSet.objects.get(id=replaces)
    except DataSet.DoesNotExist:
        return DataSetBase.objects.get_or_create(id=replaces)[0]


def _containerType_parser(container_type: dict) -> ContainerType:
    """
    Convert a dictionary about the ContainerType to a ContainerType object.

    :param container_type: Dictionary of ContainerType information.

    :return: ContainerType object.
    """
    return ContainerType.to_ContainerType(container_type)


def _keyword_parser(keywords: List[str]) -> List[Keyword]:
    """
    Convert a list of keyword strings to a list of Keyword objects.

    :param keywords: List of keyword strings.

    :return: List of Keyword obects.
    """
    return [Keyword.objects.get_or_create(name=entry)[0] for entry in keywords]


_constraints_0_3 = {
                    "content": {
                                "uuid": (str, True),
                                "replaces": (_replaces_parser, False),
                                "containerType": (_containerType_parser, True),
                                "created": (_datetime_parser, True),
                                "modified": (_datetime_parser, True),
                                "static": (bool, True),
                                "complete": (bool, True),
                                "hash": (str, False),
                                "usedSoftware": (_used_software_parser, False),
                                "modelVersion": (str, True)
                                },
                    "meta": {
                             "author": (str, True),
                             "email": (str, True),
                             "comment": (str, False),
                             "title": (str, True),
                             "keywords": (_keyword_parser, False),
                             "description": (str, False),
                             }
                    }

_constraints_0_5_1 = _constraints_0_3.copy()
_constraints_0_5_1["meta"].update({
                                   "timestamp": (str, False),
                                   "doi": (str, False),
                                   "license": (str, False),
                                   })

_constraints = {
                "0.3": _constraints_0_3,
                "0.5.1": _constraints_0_5_1
                }

MIN_SUPPORTED_VERSION = min([version.parse(k) for k in _constraints.keys()])


class BaseParser(ABC):
    """
    Base class for file format specific parsers. Parsers should inherit
    from this class because it ensures the validation of the meta data.
    """
    def __init__(self):
        self.model_version = None

    @property
    def _constraints(self) -> dict:
        """
        Return a dict containing (parser, required) pairs of the single files.

        :raises scidatacontainer_db.MetaDBError: If the model version is not
        supported.

        :return: constraints dictionary.
        """
        if not self.model_version:
            self._read_model_version()
        v = version.parse(self.model_version)
        if v < MIN_SUPPORTED_VERSION:
            raise MetaDBError({"error_code": 400,
                               "msg": "You tried to upload a dataset " +
                                      "complying scidatacontainer model " +
                                      "version " + self.model_version +
                                      " but the server requires a minimum " +
                                      "model version of " +
                                      str(MIN_SUPPORTED_VERSION)
                               })
        key = (max([k for k in _constraints.keys() if version.parse(k) < v]))
        return _constraints[key]

    @abstractmethod
    def _read_content_json(self):  # pragma: no cover
        """
        Read the content.json file and store its content to self.content.

        This method is file type specific and needs to be overwritten by every
        inheriting class.
        """
        pass

    @abstractmethod
    def _read_meta_json(self):  # pragma: no cover
        """
        Read the meta.json file and store its content to self.meta.

        This method is file type specific and needs to be overwritten by every
        inheriting class.
        """
        pass

    @abstractmethod
    def _read_filelist(self):  # pragma: no cover
        """
        Create a list of File objects and store it to self.files.

        This method is file type specific and needs to be overwritten by every
        inheriting class.
        """
        pass

    def _read_model_version(self):
        """
        Read the model version from content.json and store it as
        self.model_version.
        """
        if "modelVersion" not in self.content:  # pragma: no cover
            self._read_content_json()
        self.model_version = self.content["modelVersion"]

    def _parse_validate(self, filename: str, in_dict: dict) -> dict:
        """
        Validate the content of a dictionary using the constraints for a
        specified file.

        :param filename: Name of the file to validate. Either "meta" or
        "content".

        :param in_dict: Dictionary read from file.

        :raise scidatacontainer_db.MetaDBError: If a required item is not found
        or an error occured during parsing.

        :returns: Dictionary of validated and parsed values.
        """
        c = self._constraints[filename]
        d = {}
        for key, (t, required) in c.items():
            if required and key not in in_dict:
                raise MetaDBError({"error_code": 400,
                                   "msg": "Attribute '" + key +
                                          "' required in " + filename +
                                          ".json."
                                   })
            if key in in_dict:
                if in_dict[key] and in_dict[key] != []:
                    # convert CamelCase to snake_case (more pythonic).
                    name = re.sub(r'(?<!^)(?=[A-Z])', '_', key).lower()
                    try:
                        # try parsing
                        d[name] = t(in_dict[key])
                    except ValueError:
                        raise MetaDBError({"error_code": 400,
                                           "msg": "Failed to convert '" +
                                                  in_dict[key] + "' using " +
                                                  "the default parser. Make " +
                                                  "sure it has the right type."
                                           })
        return d

    def parse(self, filename: str, user: User) -> DataSet:
        """
        Read the meta data from the file, validate it and store it in the DB.

        :param filename: Filename of the ZDC dataset.
        :param user: User sending the request to validate permissions and to
        set ownership.
        """
        self.filename = filename
        self._read_content_json()
        self._read_meta_json()
        self._read_filelist()
        d = {"size": filename.size, "content": self.files}
        d.update(self._parse_validate("content", self.content))
        d.update(self._parse_validate("meta", self.meta))

        uuid = d["uuid"]

        if uuid.startswith("00000000-0000-0000-0000-00000000"):
            # These UUIDs are reserved for testing.
            return parse_test_data(uuid)

        del d["uuid"]
        if len(DataSetBase.objects.filter(id=uuid)) != 0:
            if len(DataSet.objects.filter(id=uuid)) != 0:
                obj = DataSet.objects.get(id=uuid)
                # existing dataset -> try to update
                return obj.update_attributes(d, user)
            else:
                obj = DataSetBase.objects.get(id=uuid)
                # previously dataset only known by ID
                # -> delete and replace with full dataset
                obj.delete()

        obj = DataSet(id=uuid)
        obj.owner = user
        obj.complete = False
        return obj.update_attributes(d, user)


class ZipContainerParser(BaseParser):
    """
    Parser implementing the file type specific routines for .ZIP based
    containers.
    """
    def _read_content_json(self):
        """
        Read the content.json file inside a .ZIP based container.
        """
        with zipfile.ZipFile(self.filename, 'r') as zfile:
            with zfile.open("content.json") as content_json:
                self.content = json.load(content_json)

    def _read_meta_json(self):
        """
        Read the meta.json file inside a .ZIP based container.
        """
        with zipfile.ZipFile(self.filename, 'r') as zfile:
            with zfile.open("meta.json") as meta_json:
                self.meta = json.load(meta_json)

    def _read_filelist(self):
        """
        Create a list of File objects inside a .ZIP container and store it to
        self.files.
        """
        self.files = []
        with zipfile.ZipFile(self.filename, 'r') as zfile:
            for _filename in zfile.namelist():
                size = zfile.getinfo(_filename).file_size
                name = _filename
                if _filename.endswith(".json"):
                    with zfile.open(_filename) as json_file:
                        data = json.load(json_file)
                        file_obj, _ = File.objects.get_or_create(name=name,
                                                                 size=size,
                                                                 content=data,
                                                                 )
                else:
                    file_obj, _ = File.objects.get_or_create(
                                    name=_filename,
                                    size=size,
                                    )
                file_obj.save()

                self.files.append(file_obj)


class Hdf5ContainerParser(BaseParser):

    def _read_content_json(self):
        raise MetaDBError({"error_code": 501,
                           "msg": "The server does not support to parse hdf5" +
                                  " files yet."
                           }
                          )

    def _read_meta_json(self):
        raise MetaDBError({"error_code": 501,
                           "msg": "The server does not support to parse hdf5" +
                                  " files yet."
                           }
                          )


def parse_container_file(filename, owner):
    """
    Find the file type, read the meta data from the file,
    validate it and store it in the DB.

    :param filename: Filename of the ZDC dataset.
    :param user: User sending the request to validate permissions and to
    set ownership.
    """
    try:
        with transaction.atomic():
            filetype = magic.from_buffer(filename.open("rb").read(2048),
                                         mime=True)
            if filetype == "application/zip":
                parser = ZipContainerParser()
                obj = parser.parse(filename, owner)
                #  obj == None for test uploads
                if not obj:
                    return
                server_path = settings.MEDIA_ROOT + "/" + str(obj.id) + ".zdc"
            elif filetype == "application/x-hdf":
                parser = Hdf5ContainerParser()
                obj = parser.parse(filename, owner)
                server_path = settings.MEDIA_ROOT + "/" + str(obj.id) + ".hdf5"
            else:
                raise MetaDBError({"error_code": 415,
                                   "msg": "File format has to be hdf5 or zip!"
                                   }
                                  )
            server_path = os.path.abspath(server_path)
            with open(server_path, 'wb+') as destination:
                for chunk in (filename).chunks():
                    destination.write(chunk)
            obj.server_path = server_path
            obj.save()
            return obj

    except MetaDBError:
        raise
    except IntegrityError as e:  # pragma: no cover
        raise MetaDBError({"error_code": 400,
                           "msg": "IntegrityError: " + str(e)})
    except ValidationError as e:
        raise MetaDBError({"error_code": 400,
                           "msg": "ValidationError: " + str(e)})
    except Exception as e:  # pragma: no cover
        raise MetaDBError({"error_code": 500,
                           "msg": "Unknown error! Please report to your " +
                           "administrator providing these information:" +
                           "\n\n" + str(e)})
