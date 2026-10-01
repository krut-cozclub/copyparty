# coding: utf-8
from __future__ import division, print_function, unicode_literals

import re
import stat
import tarfile
import time
import zipfile

from .util import Pebkac, sanitize_fn

if True:  # pylint: disable=using-constant-test
    from typing import Any, Generator, Optional


RE_DRIVE = re.compile(r"^[a-zA-Z]:")
RE_STEM = re.compile(r"(\.tar)?\.(zip|cbz|tar|tgz|tbz2?|txz|gz|bz2|xz)$", re.I)


class ArcEntry(object):
    """one member of an archive, with a validated relative path"""

    def __init__(
        self,
        rel: str,
        is_dir: bool,
        sz: int,
        mt: float,
        err: str,
        obj: Any,
    ) -> None:
        self.rel = rel  # sanitized relative path, or the raw name if err
        self.is_dir = is_dir
        self.sz = sz
        self.mt = mt
        self.err = err  # reason for skipping this entry; blank if ok
        self.obj = obj


def arc_stem(fn: str) -> str:
    """foo.tar.gz -> foo"""
    ret = RE_STEM.sub("", fn)
    return ret if ret and ret != fn else fn + ".d"


def safe_relpath(name: str) -> tuple[str, str]:
    """
    validate an archive member name; returns [relpath, error]
    rejects anything which could escape the destination folder
    """
    name = name.replace("\\", "/")
    if name.startswith("/") or RE_DRIVE.match(name):
        return "", "absolute path"

    if "\x00" in name:
        return "", "nul in filename"

    parts = []
    for part in name.split("/"):
        if part in ("", "."):
            continue

        if part == "..":
            return "", "parent-folder reference"

        part = sanitize_fn(part)
        if not part or part in (".", ".."):
            return "", "invalid filename"

        if part.lower() == ".hist":
            return "", "reserved foldername"

        parts.append(part)

    if not parts:
        return "", "blank filename"

    return "/".join(parts), ""


class ArcReader(object):
    """reads zip and tar archives, yielding validated entries"""

    def __init__(self, abspath: str) -> None:
        self.ap = abspath
        self.zf: Optional[zipfile.ZipFile] = None
        self.tf: Optional[tarfile.TarFile] = None
        self.fmt = ""

        try:
            if zipfile.is_zipfile(abspath):
                self.zf = zipfile.ZipFile(abspath, "r")
                self.fmt = "zip"
            elif tarfile.is_tarfile(abspath):
                self.tf = tarfile.open(abspath, "r:*")
                self.fmt = "tar"
        except Exception as ex:
            raise Pebkac(415, "could not open archive: %r" % (ex,))

        if not self.fmt:
            raise Pebkac(415, "not a supported archive (zip / tar / tar.gz / tar.bz2 / tar.xz)")

    def close(self) -> None:
        if self.zf:
            self.zf.close()
        if self.tf:
            self.tf.close()

    def __enter__(self) -> "ArcReader":
        return self

    def __exit__(self, *a: Any) -> None:
        self.close()

    def precheck(self) -> tuple[int, int]:
        """
        returns [num_files, total_bytes] if known without
        reading the whole archive (zip), otherwise [-1, -1]
        """
        if not self.zf:
            return -1, -1

        nf = sz = 0
        for zi in self.zf.infolist():
            if not zi.filename.endswith("/"):
                nf += 1
                sz += zi.file_size

        return nf, sz

    def entries(self) -> Generator[ArcEntry, None, None]:
        if self.zf:
            for zi in self.zf.infolist():
                yield self._zip_entry(zi)
        else:
            assert self.tf  # !rm
            for ti in self.tf:
                yield self._tar_entry(ti)

    def _zip_entry(self, zi: zipfile.ZipInfo) -> ArcEntry:
        is_dir = zi.filename.endswith("/")
        rel, err = safe_relpath(zi.filename)
        try:
            mt = time.mktime(zi.date_time + (0, 0, -1))
        except:
            mt = time.time()

        mode = zi.external_attr >> 16
        if not err and stat.S_ISLNK(mode):
            err = "symlink"
        if not err and zi.flag_bits & 1:
            err = "encrypted"

        return ArcEntry(rel or zi.filename, is_dir, zi.file_size, mt, err, zi)

    def _tar_entry(self, ti: tarfile.TarInfo) -> ArcEntry:
        rel, err = safe_relpath(ti.name)
        if not err:
            if ti.issym() or ti.islnk():
                err = "symlink"
            elif not ti.isfile() and not ti.isdir():
                err = "special file"

        return ArcEntry(rel or ti.name, ti.isdir(), ti.size, ti.mtime, err, ti)

    def open(self, ent: ArcEntry) -> Any:
        if self.zf:
            return self.zf.open(ent.obj, "r")

        assert self.tf  # !rm
        ret = self.tf.extractfile(ent.obj)
        if not ret:
            raise Pebkac(500, "tar member has no data: %r" % (ent.rel,))
        return ret
