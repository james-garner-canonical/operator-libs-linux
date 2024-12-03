# Copyright 2021 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Abstractions for the system's Debian/Ubuntu package information and repositories.

This module contains abstractions and wrappers around Debian/Ubuntu-style repositories and
packages, in order to easily provide an idiomatic and Pythonic mechanism for adding packages and/or
repositories to systems for use in machine charms.

A sane default configuration is attainable through nothing more than instantiation of the
appropriate classes. `DebianPackage` objects provide information about the architecture, version,
name, and status of a package.

`DebianPackage` will try to look up a package either from `dpkg -L` or from `apt-cache` when
provided with a string indicating the package name. If it cannot be located, `PackageNotFoundError`
will be returned, as `apt` and `dpkg` otherwise return `100` for all errors, and a meaningful error
message if the package is not known is desirable.

To install packages with convenience methods:

```python
try:
    # Run `apt-get update`
    apt.update()
    apt.add_package("zsh")
    apt.add_package(["vim", "htop", "wget"])
except PackageNotFoundError:
    logger.error("a specified package not found in package cache or on system")
except PackageError as e:
    logger.error("could not install package. Reason: %s", e.message)
````

To find details of a specific package:

```python
try:
    vim = apt.DebianPackage.from_system("vim")

    # To find from the apt cache only
    # apt.DebianPackage.from_apt_cache("vim")

    # To find from installed packages only
    # apt.DebianPackage.from_installed_package("vim")

    vim.ensure(PackageState.Latest)
    logger.info("updated vim to version: %s", vim.fullversion)
except PackageNotFoundError:
    logger.error("a specified package not found in package cache or on system")
except PackageError as e:
    logger.error("could not install package. Reason: %s", e.message)
```


`RepositoryMapping` will return a dict-like object containing enabled system repositories
and their properties (available groups, baseuri. gpg key). This class can add, disable, or
manipulate repositories. Items can be retrieved as `DebianRepository` objects.

In order add a new repository with explicit details for fields, a new `DebianRepository` can
be added to `RepositoryMapping`

`RepositoryMapping` provides an abstraction around the existing repositories on the system,
and can be accessed and iterated over like any `Mapping` object, to retrieve values by key,
iterate, or perform other operations.

Keys are constructed as `{repo_type}-{}-{release}` in order to uniquely identify a repository.

Repositories can be added with explicit values through a Python constructor.

Example:
```python
repositories = apt.RepositoryMapping()

if "deb-example.com-focal" not in repositories:
    repositories.add(DebianRepository(enabled=True, repotype="deb",
                     uri="https://example.com", release="focal", groups=["universe"]))
```

Alternatively, any valid `sources.list` line may be used to construct a new
`DebianRepository`.

Example:
```python
repositories = apt.RepositoryMapping()

if "deb-us.archive.ubuntu.com-xenial" not in repositories:
    line = "deb http://us.archive.ubuntu.com/ubuntu xenial main restricted"
    repo = DebianRepository.from_repo_line(line)
    repositories.add(repo)
```
"""

import fileinput
import glob
import itertools
import logging
import os
import re
import subprocess
import typing
from enum import Enum
from subprocess import PIPE, CalledProcessError, check_output
from typing import Any, Dict, Iterable, Iterator, List, Literal, Mapping, Optional, Tuple, Union
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# The unique Charmhub library identifier, never change it
LIBID = "7c3dbc9c2ad44a47bd6fcb25caa270e5"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 15


VALID_SOURCE_TYPES = ("deb", "deb-src")
OPTIONS_MATCHER = re.compile(r"\[.*?\]")


class Error(Exception):
    """Base class of most errors raised by this library."""

    def __repr__(self):
        """Represent the Error."""
        return "<{}.{} {}>".format(type(self).__module__, type(self).__name__, self.args)

    @property
    def name(self):
        """Return a string representation of the model plus class."""
        return "<{}.{}>".format(type(self).__module__, type(self).__name__)

    @property
    def message(self):
        """Return the message passed as an argument."""
        return self.args[0]


class PackageError(Error):
    """Raised when there's an error installing or removing a package."""


class PackageNotFoundError(Error):
    """Raised when a requested package is not known to the system."""


class PackageState(Enum):
    """A class to represent possible package states."""

    Present = "present"
    Absent = "absent"
    Latest = "latest"
    Available = "available"


class DebianPackage:
    """Represents a traditional Debian package and its utility functions.

    `DebianPackage` wraps information and functionality around a known package, whether installed
    or available. The version, epoch, name, and architecture can be easily queried and compared
    against other `DebianPackage` objects to determine the latest version or to install a specific
    version.

    The representation of this object as a string mimics the output from `dpkg` for familiarity.

    Installation and removal of packages is handled through the `state` property or `ensure`
    method, with the following options:

        apt.PackageState.Absent
        apt.PackageState.Available
        apt.PackageState.Present
        apt.PackageState.Latest

    When `DebianPackage` is initialized, the state of a given `DebianPackage` object will be set to
    `Available`, `Present`, or `Latest`, with `Absent` implemented as a convenience for removal
    (though it operates essentially the same as `Available`).
    """

    def __init__(
        self, name: str, version: str, epoch: str, arch: str, state: PackageState
    ) -> None:
        self._name = name
        self._arch = arch
        self._state = state
        self._version = Version(version, epoch)

    def __eq__(self, other) -> bool:
        """Equality for comparison.

        Args:
          other: a `DebianPackage` object for comparison

        Returns:
          A boolean reflecting equality
        """
        return isinstance(other, self.__class__) and (
            self._name,
            self._version.number,
        ) == (other._name, other._version.number)

    def __hash__(self):
        """Return a hash of this package."""
        return hash((self._name, self._version.number))

    def __repr__(self):
        """Represent the package."""
        return "<{}.{}: {}>".format(self.__module__, self.__class__.__name__, self.__dict__)

    def __str__(self):
        """Return a human-readable representation of the package."""
        return "<{}: {}-{}.{} -- {}>".format(
            self.__class__.__name__,
            self._name,
            self._version,
            self._arch,
            str(self._state),
        )

    @staticmethod
    def _apt(
        command: str,
        package_names: Union[str, List],
        optargs: Optional[List[str]] = None,
    ) -> None:
        """Wrap package management commands for Debian/Ubuntu systems.

        Args:
          command: the command given to `apt-get`
          package_names: a package name or list of package names to operate on
          optargs: an (Optional) list of additioanl arguments

        Raises:
          PackageError if an error is encountered
        """
        optargs = optargs if optargs is not None else []
        if isinstance(package_names, str):
            package_names = [package_names]
        _cmd = ["apt-get", "-y", *optargs, command, *package_names]
        try:
            env = os.environ.copy()
            env["DEBIAN_FRONTEND"] = "noninteractive"
            subprocess.run(_cmd, capture_output=True, check=True, text=True, env=env)
        except CalledProcessError as e:
            raise PackageError(
                "Could not {} package(s) [{}]: {}".format(command, [*package_names], e.stderr)
            ) from None

    def _add(self) -> None:
        """Add a package to the system."""
        self._apt(
            "install",
            "{}={}".format(self.name, self.version),
            optargs=["--option=Dpkg::Options::=--force-confold"],
        )

    def _remove(self) -> None:
        """Remove a package from the system. Implementation-specific."""
        return self._apt("remove", "{}={}".format(self.name, self.version))

    @property
    def name(self) -> str:
        """Returns the name of the package."""
        return self._name

    def ensure(self, state: PackageState):
        """Ensure that a package is in a given state.

        Args:
          state: a `PackageState` to reconcile the package to

        Raises:
          PackageError from the underlying call to apt
        """
        if self._state is not state:
            if state not in (PackageState.Present, PackageState.Latest):
                self._remove()
            else:
                self._add()
        self._state = state

    @property
    def present(self) -> bool:
        """Returns whether or not a package is present."""
        return self._state in (PackageState.Present, PackageState.Latest)

    @property
    def latest(self) -> bool:
        """Returns whether the package is the most recent version."""
        return self._state is PackageState.Latest

    @property
    def state(self) -> PackageState:
        """Returns the current package state."""
        return self._state

    @state.setter
    def state(self, state: PackageState) -> None:
        """Set the package state to a given value.

        Args:
          state: a `PackageState` to reconcile the package to

        Raises:
          PackageError from the underlying call to apt
        """
        if state in (PackageState.Latest, PackageState.Present):
            self._add()
        else:
            self._remove()
        self._state = state

    @property
    def version(self) -> "Version":
        """Returns the version for a package."""
        return self._version

    @property
    def epoch(self) -> str:
        """Returns the epoch for a package. May be unset."""
        return self._version.epoch

    @property
    def arch(self) -> str:
        """Returns the architecture for a package."""
        return self._arch

    @property
    def fullversion(self) -> str:
        """Returns the name+epoch for a package."""
        return "{}.{}".format(self._version, self._arch)

    @staticmethod
    def _get_epoch_from_version(version: str) -> Tuple[str, str]:
        """Pull the epoch, if any, out of a version string."""
        epoch_matcher = re.compile(r"^((?P<epoch>\d+):)?(?P<version>.*)")
        matches = epoch_matcher.search(version).groupdict()
        return matches.get("epoch", ""), matches.get("version")

    @classmethod
    def from_system(
        cls, package: str, version: Optional[str] = "", arch: Optional[str] = ""
    ) -> "DebianPackage":
        """Locates a package, either on the system or known to apt, and serializes the information.

        Args:
            package: a string representing the package
            version: an optional string if a specific version is requested
            arch: an optional architecture, defaulting to `dpkg --print-architecture`. If an
                architecture is not specified, this will be used for selection.

        """
        try:
            return DebianPackage.from_installed_package(package, version, arch)
        except PackageNotFoundError:
            logger.debug(
                "package '%s' is not currently installed or has the wrong architecture.", package
            )

        # Ok, try `apt-cache ...`
        try:
            return DebianPackage.from_apt_cache(package, version, arch)
        except (PackageNotFoundError, PackageError):
            # If we get here, it's not known to the systems.
            # This seems unnecessary, but virtually all `apt` commands have a return code of `100`,
            # and providing meaningful error messages without this is ugly.
            raise PackageNotFoundError(
                "Package '{}{}' could not be found on the system or in the apt cache!".format(
                    package, ".{}".format(arch) if arch else ""
                )
            ) from None

    @classmethod
    def from_installed_package(
        cls, package: str, version: Optional[str] = "", arch: Optional[str] = ""
    ) -> "DebianPackage":
        """Check whether the package is already installed and return an instance.

        Args:
            package: a string representing the package
            version: an optional string if a specific version is requested
            arch: an optional architecture, defaulting to `dpkg --print-architecture`.
                If an architecture is not specified, this will be used for selection.
        """
        system_arch = check_output(
            ["dpkg", "--print-architecture"], universal_newlines=True
        ).strip()
        arch = arch if arch else system_arch

        # Regexps are a really terrible way to do this. Thanks dpkg
        output = ""
        try:
            output = check_output(["dpkg", "-l", package], stderr=PIPE, universal_newlines=True)
        except CalledProcessError:
            raise PackageNotFoundError("Package is not installed: {}".format(package)) from None

        # Pop off the output from `dpkg -l' because there's no flag to
        # omit it`
        lines = str(output).splitlines()[5:]

        dpkg_matcher = re.compile(
            r"""
        ^(?P<package_status>\w+?)\s+
        (?P<package_name>.*?)(?P<throwaway_arch>:\w+?)?\s+
        (?P<version>.*?)\s+
        (?P<arch>\w+?)\s+
        (?P<description>.*)
        """,
            re.VERBOSE,
        )

        for line in lines:
            try:
                matches = dpkg_matcher.search(line).groupdict()
                package_status = matches["package_status"]

                if not package_status.endswith("i"):
                    logger.debug(
                        "package '%s' in dpkg output but not installed, status: '%s'",
                        package,
                        package_status,
                    )
                    break

                epoch, split_version = DebianPackage._get_epoch_from_version(matches["version"])
                pkg = DebianPackage(
                    matches["package_name"],
                    split_version,
                    epoch,
                    matches["arch"],
                    PackageState.Present,
                )
                if (pkg.arch == "all" or pkg.arch == arch) and (
                    version == "" or str(pkg.version) == version
                ):
                    return pkg
            except AttributeError:
                logger.warning("dpkg matcher could not parse line: %s", line)

        # If we didn't find it, fail through
        raise PackageNotFoundError("Package {}.{} is not installed!".format(package, arch))

    @classmethod
    def from_apt_cache(
        cls, package: str, version: Optional[str] = "", arch: Optional[str] = ""
    ) -> "DebianPackage":
        """Check whether the package is already installed and return an instance.

        Args:
            package: a string representing the package
            version: an optional string if a specific version is requested
            arch: an optional architecture, defaulting to `dpkg --print-architecture`.
                If an architecture is not specified, this will be used for selection.
        """
        system_arch = check_output(
            ["dpkg", "--print-architecture"], universal_newlines=True
        ).strip()
        arch = arch if arch else system_arch

        # Regexps are a really terrible way to do this. Thanks dpkg
        keys = ("Package", "Architecture", "Version")

        try:
            output = check_output(
                ["apt-cache", "show", package], stderr=PIPE, universal_newlines=True
            )
        except CalledProcessError as e:
            raise PackageError(
                "Could not list packages in apt-cache: {}".format(e.stderr)
            ) from None

        pkg_groups = output.strip().split("\n\n")
        keys = ("Package", "Architecture", "Version")

        for pkg_raw in pkg_groups:
            lines = str(pkg_raw).splitlines()
            vals = {}
            for line in lines:
                if line.startswith(keys):
                    items = line.split(":", 1)
                    vals[items[0]] = items[1].strip()
                else:
                    continue

            epoch, split_version = DebianPackage._get_epoch_from_version(vals["Version"])
            pkg = DebianPackage(
                vals["Package"],
                split_version,
                epoch,
                vals["Architecture"],
                PackageState.Available,
            )

            if (pkg.arch == "all" or pkg.arch == arch) and (
                version == "" or str(pkg.version) == version
            ):
                return pkg

        # If we didn't find it, fail through
        raise PackageNotFoundError("Package {}.{} is not in the apt cache!".format(package, arch))


class Version:
    """An abstraction around package versions.

    This seems like it should be strictly unnecessary, except that `apt_pkg` is not usable inside a
    venv, and wedging version comparisons into `DebianPackage` would overcomplicate it.

    This class implements the algorithm found here:
    https://www.debian.org/doc/debian-policy/ch-controlfields.html#version
    """

    def __init__(self, version: str, epoch: str):
        self._version = version
        self._epoch = epoch or ""

    def __repr__(self):
        """Represent the package."""
        return "<{}.{}: {}>".format(self.__module__, self.__class__.__name__, self.__dict__)

    def __str__(self):
        """Return human-readable representation of the package."""
        return "{}{}".format("{}:".format(self._epoch) if self._epoch else "", self._version)

    @property
    def epoch(self):
        """Returns the epoch for a package. May be empty."""
        return self._epoch

    @property
    def number(self) -> str:
        """Returns the version number for a package."""
        return self._version

    def _get_parts(self, version: str) -> Tuple[str, str]:
        """Separate the version into component upstream and Debian pieces."""
        try:
            version.rindex("-")
        except ValueError:
            # No hyphens means no Debian version
            return version, "0"

        upstream, debian = version.rsplit("-", 1)
        return upstream, debian

    def _listify(self, revision: str) -> List[str]:
        """Split a revision string into a listself.

        This list is comprised of  alternating between strings and numbers,
        padded on either end to always be "str, int, str, int..." and
        always be of even length.  This allows us to trivially implement the
        comparison algorithm described.
        """
        result = []
        while revision:
            rev_1, remains = self._get_alphas(revision)
            rev_2, remains = self._get_digits(remains)
            result.extend([rev_1, rev_2])
            revision = remains
        return result

    def _get_alphas(self, revision: str) -> Tuple[str, str]:
        """Return a tuple of the first non-digit characters of a revision."""
        # get the index of the first digit
        for i, char in enumerate(revision):
            if char.isdigit():
                if i == 0:
                    return "", revision
                return revision[0:i], revision[i:]
        # string is entirely alphas
        return revision, ""

    def _get_digits(self, revision: str) -> Tuple[int, str]:
        """Return a tuple of the first integer characters of a revision."""
        # If the string is empty, return (0,'')
        if not revision:
            return 0, ""
        # get the index of the first non-digit
        for i, char in enumerate(revision):
            if not char.isdigit():
                if i == 0:
                    return 0, revision
                return int(revision[0:i]), revision[i:]
        # string is entirely digits
        return int(revision), ""

    def _dstringcmp(self, a, b):  # noqa: C901
        """Debian package version string section lexical sort algorithm.

        The lexical comparison is a comparison of ASCII values modified so
        that all the letters sort earlier than all the non-letters and so that
        a tilde sorts before anything, even the end of a part.
        """
        if a == b:
            return 0
        try:
            for i, char in enumerate(a):
                if char == b[i]:
                    continue
                # "a tilde sorts before anything, even the end of a part"
                # (emptyness)
                if char == "~":
                    return -1
                if b[i] == "~":
                    return 1
                # "all the letters sort earlier than all the non-letters"
                if char.isalpha() and not b[i].isalpha():
                    return -1
                if not char.isalpha() and b[i].isalpha():
                    return 1
                # otherwise lexical sort
                if ord(char) > ord(b[i]):
                    return 1
                if ord(char) < ord(b[i]):
                    return -1
        except IndexError:
            # a is longer than b but otherwise equal, greater unless there are tildes
            if char == "~":
                return -1
            return 1
        # if we get here, a is shorter than b but otherwise equal, so check for tildes...
        if b[len(a)] == "~":
            return 1
        return -1

    def _compare_revision_strings(self, first: str, second: str):  # noqa: C901
        """Compare two debian revision strings."""
        if first == second:
            return 0

        # listify pads results so that we will always be comparing ints to ints
        # and strings to strings (at least until we fall off the end of a list)
        first_list = self._listify(first)
        second_list = self._listify(second)
        if first_list == second_list:
            return 0
        try:
            for i, item in enumerate(first_list):
                # explicitly raise IndexError if we've fallen off the edge of list2
                if i >= len(second_list):
                    raise IndexError
                # if the items are equal, next
                if item == second_list[i]:
                    continue
                # numeric comparison
                if isinstance(item, int):
                    if item > second_list[i]:
                        return 1
                    if item < second_list[i]:
                        return -1
                else:
                    # string comparison
                    return self._dstringcmp(item, second_list[i])
        except IndexError:
            # rev1 is longer than rev2 but otherwise equal, hence greater
            # ...except for goddamn tildes
            if first_list[len(second_list)][0][0] == "~":
                return 1
            return 1
        # rev1 is shorter than rev2 but otherwise equal, hence lesser
        # ...except for goddamn tildes
        if second_list[len(first_list)][0][0] == "~":
            return -1
        return -1

    def _compare_version(self, other) -> int:
        if (self.number, self.epoch) == (other.number, other.epoch):
            return 0

        if self.epoch < other.epoch:
            return -1
        if self.epoch > other.epoch:
            return 1

        # If none of these are true, follow the algorithm
        upstream_version, debian_version = self._get_parts(self.number)
        other_upstream_version, other_debian_version = self._get_parts(other.number)

        upstream_cmp = self._compare_revision_strings(upstream_version, other_upstream_version)
        if upstream_cmp != 0:
            return upstream_cmp

        debian_cmp = self._compare_revision_strings(debian_version, other_debian_version)
        if debian_cmp != 0:
            return debian_cmp

        return 0

    def __lt__(self, other) -> bool:
        """Less than magic method impl."""
        return self._compare_version(other) < 0

    def __eq__(self, other) -> bool:
        """Equality magic method impl."""
        return self._compare_version(other) == 0

    def __gt__(self, other) -> bool:
        """Greater than magic method impl."""
        return self._compare_version(other) > 0

    def __le__(self, other) -> bool:
        """Less than or equal to magic method impl."""
        return self.__eq__(other) or self.__lt__(other)

    def __ge__(self, other) -> bool:
        """Greater than or equal to magic method impl."""
        return self.__gt__(other) or self.__eq__(other)

    def __ne__(self, other) -> bool:
        """Not equal to magic method impl."""
        return not self.__eq__(other)


def add_package(
    package_names: Union[str, List[str]],
    version: Optional[str] = "",
    arch: Optional[str] = "",
    update_cache: Optional[bool] = False,
) -> Union[DebianPackage, List[DebianPackage]]:
    """Add a package or list of packages to the system.

    Args:
        package_names: single package name, or list of package names
        name: the name(s) of the package(s)
        version: an (Optional) version as a string. Defaults to the latest known
        arch: an optional architecture for the package
        update_cache: whether or not to run `apt-get update` prior to operating

    Raises:
        TypeError if no package name is given, or explicit version is set for multiple packages
        PackageNotFoundError if the package is not in the cache.
        PackageError if packages fail to install
    """
    cache_refreshed = False
    if update_cache:
        update()
        cache_refreshed = True

    packages = {"success": [], "retry": [], "failed": []}

    package_names = [package_names] if isinstance(package_names, str) else package_names
    if not package_names:
        raise TypeError("Expected at least one package name to add, received zero!")

    if len(package_names) != 1 and version:
        raise TypeError(
            "Explicit version should not be set if more than one package is being added!"
        )

    for p in package_names:
        pkg, success = _add(p, version, arch)
        if success:
            packages["success"].append(pkg)
        else:
            logger.warning("failed to locate and install/update '%s'", pkg)
            packages["retry"].append(p)

    if packages["retry"] and not cache_refreshed:
        logger.info("updating the apt-cache and retrying installation of failed packages.")
        update()

        for p in packages["retry"]:
            pkg, success = _add(p, version, arch)
            if success:
                packages["success"].append(pkg)
            else:
                packages["failed"].append(p)

    if packages["failed"]:
        raise PackageError("Failed to install packages: {}".format(", ".join(packages["failed"])))

    return packages["success"] if len(packages["success"]) > 1 else packages["success"][0]


def _add(
    name: str,
    version: Optional[str] = "",
    arch: Optional[str] = "",
) -> Tuple[Union[DebianPackage, str], bool]:
    """Add a package to the system.

    Args:
        name: the name(s) of the package(s)
        version: an (Optional) version as a string. Defaults to the latest known
        arch: an optional architecture for the package

    Returns: a tuple of `DebianPackage` if found, or a :str: if it is not, and
        a boolean indicating success
    """
    try:
        pkg = DebianPackage.from_system(name, version, arch)
        pkg.ensure(state=PackageState.Present)
        return pkg, True
    except PackageNotFoundError:
        return name, False


def remove_package(
    package_names: Union[str, List[str]],
    autoremove: bool = False,
) -> Union[DebianPackage, List[DebianPackage]]:
    """Remove package(s) from the system.

    Args:
        package_names: the name of a package
        autoremove: run `apt autoremove` after uninstalling packages.
            You probably want to do this if you're removing a metapackage,
            otherwise the concrete packages will still be there.
            False by default for backwards compatibility.

    Raises:
        PackageNotFoundError if the package is not found.
    """
    packages = []

    package_names = [package_names] if isinstance(package_names, str) else package_names
    if not package_names:
        raise TypeError("Expected at least one package name to add, received zero!")

    for p in package_names:
        try:
            pkg = DebianPackage.from_installed_package(p)
            pkg.ensure(state=PackageState.Absent)
            packages.append(pkg)
        except PackageNotFoundError:
            logger.info("package '%s' was requested for removal, but it was not installed.", p)

    if autoremove:
        subprocess.run(["apt", "autoremove"], check=True)

    # the list of packages will be empty when no package is removed
    logger.debug("packages: '%s'", packages)
    return packages[0] if len(packages) == 1 else packages


def update() -> None:
    """Update the apt cache via `apt-get update`."""
    cmd = ["apt-get", "update", "--error-on=any"]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
    except CalledProcessError as e:
        logger.error(
            "%s:\nstdout:\n%s\nstderr:\n%s",
            " ".join(cmd),
            e.stdout.decode(),
            e.stderr.decode(),
        )
        raise


def import_key(key: str) -> str:
    """Import an ASCII Armor key.

    A Radix64 format keyid is also supported for backwards
    compatibility. In this case Ubuntu keyserver will be
    queried for a key via HTTPS by its keyid. This method
    is less preferable because https proxy servers may
    require traffic decryption which is equivalent to a
    man-in-the-middle attack (a proxy server impersonates
    keyserver TLS certificates and has to be explicitly
    trusted by the system).

    Args:
        key: A GPG key in ASCII armor format, including BEGIN
            and END markers or a keyid.

    Returns:
        The GPG key filename written.

    Raises:
        GPGKeyError if the key could not be imported
    """
    key = key.strip()
    if "-" in key or "\n" in key:
        # Send everything not obviously a keyid to GPG to import, as
        # we trust its validation better than our own. eg. handling
        # comments before the key.
        logger.debug("PGP key found (looks like ASCII Armor format)")
        if (
            "-----BEGIN PGP PUBLIC KEY BLOCK-----" in key
            and "-----END PGP PUBLIC KEY BLOCK-----" in key
        ):
            logger.debug("Writing provided PGP key in the binary format")
            key_bytes = key.encode("utf-8")
            key_name = DebianRepository._get_keyid_by_gpg_key(key_bytes)
            key_gpg = DebianRepository._dearmor_gpg_key(key_bytes)
            gpg_key_filename = "/etc/apt/trusted.gpg.d/{}.gpg".format(key_name)
            DebianRepository._write_apt_gpg_keyfile(
                key_name=gpg_key_filename, key_material=key_gpg
            )
            return gpg_key_filename
        else:
            raise GPGKeyError("ASCII armor markers missing from GPG key")
    else:
        logger.warning(
            "PGP key found (looks like Radix64 format). "
            "SECURELY importing PGP key from keyserver; "
            "full key not provided."
        )
        # as of bionic add-apt-repository uses curl with an HTTPS keyserver URL
        # to retrieve GPG keys. `apt-key adv` command is deprecated as is
        # apt-key in general as noted in its manpage. See lp:1433761 for more
        # history. Instead, /etc/apt/trusted.gpg.d is used directly to drop
        # gpg
        key_asc = DebianRepository._get_key_by_keyid(key)
        # write the key in GPG format so that apt-key list shows it
        key_gpg = DebianRepository._dearmor_gpg_key(key_asc.encode("utf-8"))
        gpg_key_filename = "/etc/apt/trusted.gpg.d/{}.gpg".format(key)
        DebianRepository._write_apt_gpg_keyfile(key_name=gpg_key_filename, key_material=key_gpg)
        return gpg_key_filename


class InvalidSourceError(Error):
    """Exceptions for invalid source entries."""


class GPGKeyError(Error):
    """Exceptions for GPG keys."""


class DebianRepository:
    """An abstraction to represent a repository."""

    _deb822_stanza: Optional["_Deb822Stanza"] = None
    """set by Deb822Stanza after creating a DebianRepository"""

    def __init__(
        self,
        enabled: bool,
        repotype: str,
        uri: str,
        release: str,
        groups: List[str],
        filename: Optional[str] = "",
        gpg_key_filename: Optional[str] = "",
        options: Optional[Dict[str, str]] = None,
    ):
        self._enabled = enabled
        self._repotype = repotype
        self._uri = uri
        self._release = release
        self._groups = groups
        self._filename = filename
        self._gpg_key_filename = gpg_key_filename
        self._options = options

    @property
    def enabled(self):
        """Return whether or not the repository is enabled."""
        return self._enabled

    @property
    def repotype(self):
        """Return whether it is binary or source."""
        return self._repotype

    @property
    def uri(self):
        """Return the URI."""
        return self._uri

    @property
    def release(self):
        """Return which Debian/Ubuntu releases it is valid for."""
        return self._release

    @property
    def groups(self):
        """Return the enabled package groups."""
        return self._groups

    @property
    def filename(self):
        """Returns the filename for a repository."""
        return self._filename

    @filename.setter
    def filename(self, fname: str) -> None:
        """Set the filename used when a repo is written back to disk.

        Args:
            fname: a filename to write the repository information to.
        """
        if not fname.endswith(".list") or fname.endswith(".sources"):
            raise InvalidSourceError("apt source filenames should end in .list or .sources!")
        self._filename = fname

    @property
    def gpg_key(self):
        """Returns the path to the GPG key for this repository."""
        if not self._gpg_key_filename and self._deb822_stanza is not None:
            self._gpg_key_filename = self._deb822_stanza.get_gpg_key_filename()
        return self._gpg_key_filename

    @property
    def options(self):
        """Returns any additional repo options which are set."""
        return self._options

    def make_options_string(self) -> str:
        """Generate the complete one-line-style options string for a repository.

        Combining `gpg_key`, if set, and the rest of the options to find
        a complex repo string.
        """
        options = self._options if self._options else {}

        gpg_key_filename = self.gpg_key  # ensure getter logic is run
        if gpg_key_filename:
            options["signed-by"] = gpg_key_filename

        if not options:
            return ""

        return "[{}] ".format(" ".join("{}={}".format(k, v) for k, v in options.items()))

    @staticmethod
    def prefix_from_uri(uri: str) -> str:
        """Get a repo list prefix from the uri, depending on whether a path is set."""
        uridetails = urlparse(uri)
        path = (
            uridetails.path.lstrip("/").replace("/", "-") if uridetails.path else uridetails.netloc
        )
        return "/etc/apt/sources.list.d/{}".format(path)

    @staticmethod
    def from_repo_line(repo_line: str, write_file: Optional[bool] = True) -> "DebianRepository":
        """Instantiate a new `DebianRepository` from a `sources.list` entry line.

        Args:
            repo_line: a string representing a repository entry
            write_file: boolean to enable writing the new repo to disk
        """
        repo = RepositoryMapping._parse(repo_line, "UserInput")
        fname = "{}-{}.list".format(
            DebianRepository.prefix_from_uri(repo.uri), repo.release.replace("/", "-")
        )
        repo.filename = fname

        options = repo.options if repo.options else {}
        if repo.gpg_key:
            options["signed-by"] = repo.gpg_key

        # For Python 3.5 it's required to use sorted in the options dict in order to not have
        # different results in the order of the options between executions.
        options_str = (
            "[{}] ".format(" ".join(["{}={}".format(k, v) for k, v in sorted(options.items())]))
            if options
            else ""
        )

        repo_str = (
            "{}".format("#" if not repo.enabled else "")
            + "{} {}{} ".format(repo.repotype, options_str, repo.uri)
            + "{} {}\n".format(repo.release, " ".join(repo.groups))
        )

        if write_file:
            logger.info(
                "DebianRepository.from_repo_line('%s', write_file=True)\nWriting to '%s':\n%s",
                repo_line,
                fname,
                repo_str,
            )
            with open(fname, "wb") as f:
                f.write(repo_str.encode("utf-8"))

        return repo

    def disable(self) -> None:
        """Remove this repository by disabling it in the source file.

        WARNING: This method does NOT alter the `self.enabled` flag.

        WARNING: disable is currently not implemented for repositories defined
        by a deb822 stanza. Raises a NotImplementedError in this case.
        """
        if self._deb822_stanza is not None:
            raise NotImplementedError(
                "Disabling a repository defined by a deb822 format source is not implemented."
                " Please raise an issue if you require this feature."
            )
        searcher = "{} {}{} {}".format(
            self.repotype, self.make_options_string(), self.uri, self.release
        )
        for line in fileinput.input(self._filename, inplace=True):
            if re.match(r"^{}\s".format(re.escape(searcher)), line):
                print("# {}".format(line), end="")
            else:
                print(line, end="")

    def import_key(self, key: str) -> None:
        """Import an ASCII Armor key.

        A Radix64 format keyid is also supported for backwards
        compatibility. In this case Ubuntu keyserver will be
        queried for a key via HTTPS by its keyid. This method
        is less preferable because https proxy servers may
        require traffic decryption which is equivalent to a
        man-in-the-middle attack (a proxy server impersonates
        keyserver TLS certificates and has to be explicitly
        trusted by the system).

        Args:
          key: A GPG key in ASCII armor format,
                      including BEGIN and END markers or a keyid.

        Raises:
          GPGKeyError if the key could not be imported
        """
        self._gpg_key_filename = import_key(key)

    @staticmethod
    def _get_keyid_by_gpg_key(key_material: bytes) -> str:
        """Get a GPG key fingerprint by GPG key material.

        Gets a GPG key fingerprint (40-digit, 160-bit) by the ASCII armor-encoded
        or binary GPG key material. Can be used, for example, to generate file
        names for keys passed via charm options.
        """
        # Use the same gpg command for both Xenial and Bionic
        cmd = ["gpg", "--with-colons", "--with-fingerprint"]
        ps = subprocess.run(
            cmd,
            stdout=PIPE,
            stderr=PIPE,
            input=key_material,
        )
        out, err = ps.stdout.decode(), ps.stderr.decode()
        if "gpg: no valid OpenPGP data found." in err:
            raise GPGKeyError("Invalid GPG key material provided")
        # from gnupg2 docs: fpr :: Fingerprint (fingerprint is in field 10)
        return re.search(r"^fpr:{9}([0-9A-F]{40}):$", out, re.MULTILINE).group(1)

    @staticmethod
    def _get_key_by_keyid(keyid: str) -> str:
        """Get a key via HTTPS from the Ubuntu keyserver.

        Different key ID formats are supported by SKS keyservers (the longer ones
        are more secure, see "dead beef attack" and https://evil32.com/). Since
        HTTPS is used, if SSLBump-like HTTPS proxies are in place, they will
        impersonate keyserver.ubuntu.com and generate a certificate with
        keyserver.ubuntu.com in the CN field or in SubjAltName fields of a
        certificate. If such proxy behavior is expected it is necessary to add the
        CA certificate chain containing the intermediate CA of the SSLBump proxy to
        every machine that this code runs on via ca-certs cloud-init directive (via
        cloudinit-userdata model-config) or via other means (such as through a
        custom charm option). Also note that DNS resolution for the hostname in a
        URL is done at a proxy server - not at the client side.
        8-digit (32 bit) key ID
        https://keyserver.ubuntu.com/pks/lookup?search=0x4652B4E6
        16-digit (64 bit) key ID
        https://keyserver.ubuntu.com/pks/lookup?search=0x6E85A86E4652B4E6
        40-digit key ID:
        https://keyserver.ubuntu.com/pks/lookup?search=0x35F77D63B5CEC106C577ED856E85A86E4652B4E6

        Args:
          keyid: An 8, 16 or 40 hex digit keyid to find a key for

        Returns:
          A string contining key material for the specified GPG key id


        Raises:
          subprocess.CalledProcessError
        """
        # options=mr - machine-readable output (disables html wrappers)
        keyserver_url = (
            "https://keyserver.ubuntu.com" "/pks/lookup?op=get&options=mr&exact=on&search=0x{}"
        )
        curl_cmd = ["curl", keyserver_url.format(keyid)]
        # use proxy server settings in order to retrieve the key
        return check_output(curl_cmd).decode()

    @staticmethod
    def _dearmor_gpg_key(key_asc: bytes) -> bytes:
        """Convert a GPG key in the ASCII armor format to the binary format.

        Args:
          key_asc: A GPG key in ASCII armor format.

        Returns:
          A GPG key in binary format as a string

        Raises:
          GPGKeyError
        """
        ps = subprocess.run(["gpg", "--dearmor"], stdout=PIPE, stderr=PIPE, input=key_asc)
        out, err = ps.stdout, ps.stderr.decode()
        if "gpg: no valid OpenPGP data found." in err:
            raise GPGKeyError(
                "Invalid GPG key material. Check your network setup"
                " (MTU, routing, DNS) and/or proxy server settings"
                " as well as destination keyserver status."
            )
        else:
            return out

    @staticmethod
    def _write_apt_gpg_keyfile(key_name: str, key_material: bytes) -> None:
        """Write GPG key material into a file at a provided path.

        Args:
          key_name: A key name to use for a key file (could be a fingerprint)
          key_material: A GPG key material (binary)
        """
        with open(key_name, "wb") as keyf:
            keyf.write(key_material)


class RepositoryMapping(Mapping[str, DebianRepository]):
    """An representation of known repositories.

    Instantiation of `RepositoryMapping` will iterate through the
    filesystem, parse out repository files in `/etc/apt/...`, and create
    `DebianRepository` objects in this list.

    Typical usage:

        repositories = apt.RepositoryMapping()
        repositories.add(DebianRepository(
            enabled=True, repotype="deb", uri="https://example.com", release="focal",
            groups=["universe"]
        ))
    """

    _apt_dir = "/etc/apt"
    _sources_subdir = "sources.list.d"
    _default_list_name = "sources.list"
    _default_sources_name = "ubuntu.sources"

    def __init__(self):
        self._repository_map: Dict[str, DebianRepository] = {}
        self.default_file = os.path.join(self._apt_dir, self._default_list_name)
        # ^ public attribute for backwards compatibility only
        sources_dir = os.path.join(self._apt_dir, self._sources_subdir)
        default_sources = os.path.join(sources_dir, self._default_sources_name)

        # read sources.list if it exists
        # ignore InvalidSourceError if ubuntu.sources also exists
        # -- in this case, sources.list just contains a comment
        if os.path.isfile(self.default_file):
            try:
                self.load(self.default_file)
            except InvalidSourceError:
                if not os.path.isfile(default_sources):
                    raise

        # read sources.list.d
        for file in glob.iglob(os.path.join(sources_dir, "*.list")):
            self.load(file)
        for file in glob.iglob(os.path.join(sources_dir, "*.sources")):
            self.load_deb822(file)

    def __contains__(self, key: Any) -> bool:
        """Magic method for checking presence of repo in mapping.

        Checks against the string names used to identify repositories.
        """
        return key in self._repository_map

    def __len__(self) -> int:
        """Return number of repositories in map."""
        return len(self._repository_map)

    def __iter__(self) -> Iterable[DebianRepository]:
        """Return iterator for RepositoryMapping.

        Iterates over the DebianRepository values rather than the string names.
        """
        return iter(self._repository_map.values())

    def __getitem__(self, repository_uri: str) -> DebianRepository:
        """Return a given `DebianRepository`."""
        return self._repository_map[repository_uri]

    def __setitem__(self, repository_uri: str, repository: DebianRepository) -> None:
        """Add a `DebianRepository` to the cache."""
        self._repository_map[repository_uri] = repository

    def load_deb822(self, filename: str) -> None:
        """Load a deb822 format repository source file into the cache.

        In contrast to one-line-style, the deb822 format specifies a repository
        using a multi-line paragraph. Paragraphs are separated by whitespace,
        and each definition consists of lines that are either key: value pairs,
        or continuations of the previous value.

        Read more about the deb822 format here:
            https://manpages.ubuntu.com/manpages/noble/en/man5/sources.list.5.html
        For instance, ubuntu 24.04 (noble) lists its sources using deb822 style in:
            /etc/apt/sources.list.d/ubuntu.sources
        """
        with open(filename, "r") as f:
            repos, errors = self._parse_deb822_lines(f, filename=filename)

        for repo in repos:
            repo_identifier = "{}-{}-{}".format(repo.repotype, repo.uri, repo.release)
            self._repository_map[repo_identifier] = repo

        if errors:
            logger.debug(
                "the following %d error(s) were encountered when reading deb822 sources:\n%s",
                len(errors),
                "\n".join(str(e) for e in errors),
            )

        if repos:
            logger.info("parsed %d apt package repositories", len(repos))
        else:
            raise InvalidSourceError("all repository lines in '{}' were invalid!".format(filename))

    @classmethod
    def _parse_deb822_lines(
        cls,
        lines: Iterable[str],
        filename: str = "",
    ) -> Tuple[List[DebianRepository], List[InvalidSourceError]]:
        """Parse lines from a deb822 file into a list of repos and a list of errors.

        The semantics of `_parse_deb822_lines` slightly different to `_parse`:
            `_parse` reads a commented out line as an entry that is not enabled
            `_parse_deb822_lines` strips out comments entirely when parsing a file into stanzas,
                instead only reading the 'Enabled' key to determine if an entry is enabled
        """
        repositories: List[DebianRepository] = []
        errors: List[InvalidSourceError] = []
        for numbered_lines in _iter_deb822_stanzas(lines):
            try:
                stanza = _Deb822Stanza(numbered_lines=numbered_lines, filename=filename)
            except InvalidSourceError as e:
                errors.append(e)
            else:
                repositories.extend(stanza.repositories)
        return repositories, errors

    def load(self, filename: str):
        """Load a one-line-style format repository source file into the cache.

        Args:
          filename: the path to the repository file
        """
        parsed: List[int] = []
        skipped: List[int] = []
        with open(filename, "r") as f:
            for n, line in enumerate(f, start=1):  # 1 indexed line numbers
                try:
                    repo = self._parse(line, filename)
                except InvalidSourceError:
                    skipped.append(n)
                else:
                    repo_identifier = "{}-{}-{}".format(repo.repotype, repo.uri, repo.release)
                    self._repository_map[repo_identifier] = repo
                    parsed.append(n)
                    logger.debug("parsed repo: '%s'", repo_identifier)

        if skipped:
            skip_list = ", ".join(str(s) for s in skipped)
            logger.debug("skipped the following lines in file '%s': %s", filename, skip_list)

        if parsed:
            logger.info("parsed %d apt package repositories", len(parsed))
        else:
            raise InvalidSourceError("all repository lines in '{}' were invalid!".format(filename))

    @staticmethod
    def _parse(line: str, filename: str) -> DebianRepository:
        """Parse a line in a sources.list file.

        Args:
          line: a single line from `load` to parse
          filename: the filename being read

        Raises:
          InvalidSourceError if the source type is unknown
        """
        enabled = True
        repotype = uri = release = gpg_key = ""
        options = {}
        groups = []

        line = line.strip()
        if line.startswith("#"):
            enabled = False
            line = line[1:]

        # Check for "#" in the line and treat a part after it as a comment then strip it off.
        i = line.find("#")
        if i > 0:
            line = line[:i]

        # Split a source into substrings to initialize a new repo.
        source = line.strip()
        if source:
            # Match any repo options, and get a dict representation.
            for v in re.findall(OPTIONS_MATCHER, source):
                opts = dict(o.split("=") for o in v.strip("[]").split())
                # Extract the 'signed-by' option for the gpg_key
                gpg_key = opts.pop("signed-by", "")
                options = opts

            # Remove any options from the source string and split the string into chunks
            source = re.sub(OPTIONS_MATCHER, "", source)
            chunks = source.split()

            # Check we've got a valid list of chunks
            if len(chunks) < 3 or chunks[0] not in VALID_SOURCE_TYPES:
                raise InvalidSourceError("An invalid sources line was found in %s!", filename)

            repotype = chunks[0]
            uri = chunks[1]
            release = chunks[2]
            groups = chunks[3:]

            return DebianRepository(
                enabled, repotype, uri, release, groups, filename, gpg_key, options
            )
        else:
            raise InvalidSourceError("An invalid sources line was found in %s!", filename)

    @typing.overload
    def add(  # pyright: ignore[reportOverlappingOverload]
        self,
        repo: DebianRepository,
        default_filename: Optional[bool] = False,
        update_cache: Literal[False] = False,
    ) -> None: ...
    @typing.overload
    def add(
        self,
        repo: DebianRepository,
        default_filename: Optional[bool] = False,
        update_cache: Literal[True] = True,
    ) -> "RepositoryMapping": ...
    def add(  # noqa: D417  # undocumented-param: default_filename intentionally undocumented
        self,
        repo: DebianRepository,
        default_filename: Optional[bool] = False,
        update_cache: bool = False,
    ) -> Optional["RepositoryMapping"]:
        """Add a new repository to the system using apt-add-repository.

        Args:
            repo: a DebianRepository object where repo.enabled is True
            update_cache: if True, apt-add-repository will update the package cache
                and then a new RepositoryMapping object will be returned.
                If False, then apt-add-repository is run with the --no-update option,
                and an entry for repo is added to this RepositoryMapping, and you
                can call `update` manually before installing any packages.

        Returns:
            None, or a new RepositoryMapping object if update_cache is True

        raises:
            ValueError: if repo.enabled is False
            CalledProcessError: if there's an error running apt-add-repository

        WARNING: Does not associate the repository with a signing key.
        Use the `import_key` method to add a signing key globally.

        WARNING: the default_filename keyword argument is provided for backwards compatibility
        only. It is not used, and was not used in the previous revision of this library.
        """
        if not repo.enabled:
            raise ValueError("{repo}.enabled is {value}".format(repo=repo, value=repo.enabled))
        cmd = [
            "apt-add-repository",
            "--dry-run",
            "--yes",
            f"--uri={repo.uri}",
            f"--pocket={repo.release}",
        ]
        if repo.repotype == "deb-src":
            cmd.append("--enable-source")
        for component in repo.groups:
            cmd.append(f"--component={component}")
        if not update_cache:
            cmd.append("--no-update")
        subprocess.run(cmd, check=True)
        if update_cache:
            return RepositoryMapping()
        self._repository_map["{}-{}-{}".format(repo.repotype, repo.uri, repo.release)] = repo

    def disable(self, repo: DebianRepository) -> None:
        """Remove a repository by disabling it in the source file.

        WARNING: disable is currently not implemented for repositories defined
        by a deb822 stanza, and will raise a NotImplementedError if called on one.

        WARNING: This method does NOT alter the `.enabled` flag on the DebianRepository.
        """
        repo.disable()


class _Deb822Stanza:
    """Representation of a stanza from a deb822 source file.

    May define multiple DebianRepository objects.
    """

    def __init__(self, numbered_lines: List[Tuple[int, str]], filename: str = ""):
        self._numbered_lines = numbered_lines
        self._filename = filename
        self._options, self._line_numbers = _deb822_stanza_to_options(numbered_lines)
        repos, gpg_key_info = _deb822_options_to_repos(
            self._options, line_numbers=self._line_numbers, filename=filename
        )
        for repo in repos:
            repo._deb822_stanza = self  # pyright: ignore[reportPrivateUsage]
        self._repositories = repos
        self._gpg_key_filename, self._gpg_key_from_stanza = gpg_key_info

    @property
    def repositories(self) -> Tuple[DebianRepository, ...]:
        """The repositories defined by this deb822 stanza."""
        return self._repositories

    def get_gpg_key_filename(self) -> str:
        """Return the path to the GPG key for this repository.

        Import the key first, if the key itself was provided in the stanza.
        """
        if self._gpg_key_filename:
            return self._gpg_key_filename
        if self._gpg_key_from_stanza is None:
            return ""
        # a gpg key was provided in the stanza
        # and we haven't already imported it
        self._gpg_key_filename = import_key(self._gpg_key_from_stanza)
        return self._gpg_key_filename


def _iter_deb822_stanzas(lines: Iterable[str]) -> Iterator[List[Tuple[int, str]]]:
    """Given lines from a deb822 format file, yield a stanza of lines.

    Args:
        lines: an iterable of lines from a deb822 sources file

    Yields:
        lists of numbered lines (a tuple of line number and line) that make up
        a deb822 stanza, with comments stripped out (but accounted for in line numbering)
    """
    current_paragraph: List[Tuple[int, str]] = []
    for n, line in enumerate(lines, start=1):  # 1 indexed line numbers
        if not line.strip():  # blank lines separate paragraphs
            if current_paragraph:
                yield current_paragraph
                current_paragraph = []
            continue
        content, _delim, _comment = line.partition("#")
        if content.strip():  # skip (potentially indented) comment line
            current_paragraph.append((n, content.rstrip()))  # preserve indent
    if current_paragraph:
        yield current_paragraph


def _deb822_stanza_to_options(
    lines: Iterable[Tuple[int, str]],
) -> Tuple[Dict[str, str], Dict[str, int]]:
    """Turn numbered lines into a dict of options and a dict of line numbers.

    Args:
        lines: an iterable of numbered lines (a tuple of line number and line)

    Returns:
        a dictionary of option names to (potentially multiline) values, and
        a dictionary of option names to starting line number
    """
    parts: Dict[str, List[str]] = {}
    line_numbers: Dict[str, int] = {}
    current = None
    for n, line in lines:
        assert "#" not in line  # comments should be stripped out
        if line.startswith(" "):  # continuation of previous key's value
            assert current is not None
            parts[current].append(line.rstrip())  # preserve indent
            continue
        raw_key, _, raw_value = line.partition(":")
        current = raw_key.strip()
        parts[current] = [raw_value.strip()]
        line_numbers[current] = n
    options = {k: "\n".join(v) for k, v in parts.items()}
    return options, line_numbers


def _deb822_options_to_repos(
    options: Dict[str, str], line_numbers: Mapping[str, int] = {}, filename: str = ""
) -> Tuple[Tuple[DebianRepository, ...], Tuple[str, Optional[str]]]:
    """Return a collections of DebianRepository objects defined by this deb822 stanza.

    Args:
        options: a dictionary of deb822 field names to string options
        line_numbers: a dictionary of field names to line numbers (for error messages)
        filename: the file the options were read from (for repository object and errors)

    Returns:
        a tuple of `DebianRepository`s, and
        a tuple of the gpg key filename and optional in-stanza provided key itself

    Raises:
      InvalidSourceError if any options are malformed or required options are missing
    """
    # Enabled
    enabled_field = options.pop("Enabled", "yes")
    if enabled_field == "yes":
        enabled = True
    elif enabled_field == "no":
        enabled = False
    else:
        raise InvalidSourceError(
            (
                "Bad value '{value}' for entry 'Enabled' (line {enabled_line})"
                " in file {file}. If 'Enabled' is present it must be one of"
                " yes or no (if absent it defaults to yes)."
            ).format(
                value=enabled_field,
                enabled_line=line_numbers.get("Enabled"),
                file=filename,
            )
        )
    # Signed-By
    gpg_key_file = options.pop("Signed-By", "")
    gpg_key_from_stanza: Optional[str] = None
    if "\n" in gpg_key_file:
        # actually a literal multi-line gpg-key rather than a filename
        gpg_key_from_stanza = gpg_key_file
        gpg_key_file = ""
    # Types
    try:
        repotypes = options.pop("Types").split()
        uris = options.pop("URIs").split()
        suites = options.pop("Suites").split()
    except KeyError as e:
        [key] = e.args
        raise InvalidSourceError(
            "Missing required entry '{key}' for entry starting on line {line} in {file}.".format(
                key=key,
                line=min(line_numbers.values()) if line_numbers else None,
                file=filename,
            )
        )
    # Components
    components: List[str]
    if len(suites) == 1 and suites[0].endswith("/"):
        if "Components" in options:
            raise InvalidSourceError(
                (
                    "Since 'Suites' (line {suites_line}) specifies"
                    " a path relative to  'URIs' (line {uris_line}),"
                    " 'Components' (line {components_line}) must be  ommitted"
                    " (in file {file})."
                ).format(
                    suites_line=line_numbers.get("Suites"),
                    uris_line=line_numbers.get("URIs"),
                    components_line=line_numbers.get("Components"),
                    file=filename,
                )
            )
        components = []
    else:
        if "Components" not in options:
            raise InvalidSourceError(
                (
                    "Since 'Suites' (line {suites_line}) does not specify"
                    " a path relative to  'URIs' (line {uris_line}),"
                    " 'Components' must be  present in this paragraph"
                    " (in file {file})."
                ).format(
                    suites_line=line_numbers.get("Suites"),
                    uris_line=line_numbers.get("URIs"),
                    file=filename,
                )
            )
        components = options.pop("Components").split()
    repos = tuple(
        DebianRepository(
            enabled=enabled,
            repotype=repotype,
            uri=uri,
            release=suite,
            groups=components,
            filename=filename,
            gpg_key_filename=gpg_key_file,
            options=options,
        )
        for repotype, uri, suite in itertools.product(repotypes, uris, suites)
    )
    return repos, (gpg_key_file, gpg_key_from_stanza)
