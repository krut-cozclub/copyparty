#!/usr/bin/env python3
# coding: utf-8
from __future__ import division, print_function, unicode_literals

import io
import json
import os
import shutil
import stat
import tarfile
import tempfile
import time
import unittest
import zipfile

from copyparty.authsrv import AuthSrv
from copyparty.httpcli import HttpCli
from copyparty.unarc import arc_stem, safe_relpath
from tests import util as tu
from tests.util import Cfg


def mkzip(path, members):
    """members: list of (name, data) or (ZipInfo, data)"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)


def mktar(path, members, mode="w:gz"):
    """members: list of (TarInfo-kwargs, data)"""
    with tarfile.open(path, mode) as tf:
        for ka, data in members:
            ti = tarfile.TarInfo(ka.pop("name"))
            for k, v in ka.items():
                setattr(ti, k, v)
            if data is None:
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))


class TestUnarcPaths(unittest.TestCase):
    def test_safe_relpath(self):
        ok = [
            ("a.txt", "a.txt"),
            ("d/e/f.txt", "d/e/f.txt"),
            ("./d//e/./f", "d/e/f"),
            ("d\\e\\f", "d/e/f"),
            ("dir/", "dir"),
        ]
        for src, exp in ok:
            self.assertEqual(safe_relpath(src), (exp, ""), src)

        bad = [
            "../x",
            "a/../../x",
            "a/..",
            "..\\x",
            "/etc/passwd",
            "\\\\srv\\share",
            "C:/windows/x",
            "c:x",
            ".hist/up2k.db",
            "a/.HIST/th/x",
            "",
            "./",
            "a\x00b",
        ]
        for src in bad:
            rel, err = safe_relpath(src)
            self.assertEqual(rel, "", src)
            self.assertTrue(err, src)

    def test_stem(self):
        self.assertEqual(arc_stem("foo.zip"), "foo")
        self.assertEqual(arc_stem("foo.tar.gz"), "foo")
        self.assertEqual(arc_stem("foo.TGZ"), "foo")
        self.assertEqual(arc_stem("a.b.tar.xz"), "a.b")
        self.assertEqual(arc_stem("weird"), "weird.d")
        self.assertEqual(arc_stem(".zip"), ".zip.d")


class TestUnzip(unittest.TestCase):
    def setUp(self):
        self.td = tu.get_ramdisk()
        self.conn = None

    def tearDown(self):
        if self.conn:
            self.conn.shutdown()
        os.chdir(tempfile.gettempdir())
        shutil.rmtree(self.td)

    def log(self, src, msg, c=0):
        print(("[%s] %s" % (src, msg)).encode("ascii", "replace").decode("ascii"))

    def reset(self, **ka):
        if self.conn:
            self.conn.shutdown()
            self.conn = None
        os.chdir(self.td)
        td = os.path.join(self.td, "vfs")
        if os.path.exists(td):
            shutil.rmtree(td)
        os.mkdir(td)
        os.chdir(td)
        os.mkdir("w")  # writable by u1 and u2
        os.mkdir("r")  # read-only for u1
        os.mkdir("outside")  # not shared

        # u1 = read+write, u2 = read+write+delete, u3 = read-only
        vols = [
            "w:w:r,u3:rw,u1:rwd,u2",
            "r:r:r,u1,u2,u3",
        ]
        self.args = Cfg(v=vols, a=["u1:p1", "u2:p2", "u3:p3"], **ka)
        self.asrv = AuthSrv(self.args, self.log)
        self.conn = tu.VHttpConn(self.args, self.asrv, self.log, b"", True)
        return td

    def unzip(self, vp, dst=None, pw="p1", extra=""):
        url = "/%s?unzip" % (vp,)
        if dst is not None:
            url += "=" + dst
        url += extra
        hdr = "POST %s HTTP/1.1\r\nPW: %s\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
        buf = (hdr % (url, pw)).encode("utf-8")
        HttpCli(self.conn.setbuf(buf)).run()
        h, b = self.conn.s._reply.decode("utf-8").split("\r\n\r\n", 1)
        code = int(h.split(" ")[1])
        print("UNZIP %s --> %d %s" % (url, code, b[:400]))
        if code == 201:
            return code, json.loads(b)
        return code, b

    def read(self, fp):
        with open(fp, "rb") as f:
            return f.read()

    def test_basic(self):
        self.reset()
        mkzip("w/a.zip", [("x.txt", b"hello"), ("sub/y.txt", b"world"), ("emptydir/", b"")])

        code, ret = self.unzip("w/a.zip")
        self.assertEqual(code, 201)
        self.assertEqual(ret["dst"], "/w/a/")
        self.assertEqual(ret["nf"], 2)
        self.assertEqual(ret["nskip"], 0)
        self.assertEqual(ret["sz"], 10)
        self.assertEqual(self.read("w/a/x.txt"), b"hello")
        self.assertEqual(self.read("w/a/sub/y.txt"), b"world")
        self.assertTrue(os.path.isdir("w/a/emptydir"))
        # no leftover temp files
        self.assertEqual(sorted(os.listdir("w/a")), ["emptydir", "sub", "x.txt"])

        # explicit destination, and mtime is taken from the archive
        zi = zipfile.ZipInfo("old.txt", (2001, 2, 3, 4, 5, 6))
        mkzip("w/b.zip", [(zi, b"old")])
        code, ret = self.unzip("w/b.zip", "/w/elsewhere/deeper")
        self.assertEqual(code, 201)
        self.assertEqual(self.read("w/elsewhere/deeper/old.txt"), b"old")
        mt = time.localtime(os.path.getmtime("w/elsewhere/deeper/old.txt"))
        self.assertEqual(mt[:3], (2001, 2, 3))

    def test_traversal(self):
        self.reset()
        evil = [
            ("../../outside/pwn1", b"x"),
            ("ok/../../../outside/pwn2", b"x"),
            ("/abs/pwn3", b"x"),
            ("C:/pwn4", b"x"),
            ("..\\..\\outside\\pwn5", b"x"),
            (".hist/up2k.db", b"x"),
            ("good.txt", b"fine"),
        ]
        mkzip("w/evil.zip", evil)
        code, ret = self.unzip("w/evil.zip", "w")
        self.assertEqual(code, 201)
        self.assertEqual(ret["nf"], 1)
        self.assertEqual(ret["nskip"], 6)
        self.assertEqual(self.read("w/good.txt"), b"fine")
        self.assertEqual(os.listdir("outside"), [])
        self.assertFalse(os.path.exists("w/.hist/up2k.db"))
        for root, dirs, files in os.walk(self.td):
            for fn in files:
                self.assertFalse(fn.startswith("pwn"), os.path.join(root, fn))

        # destination itself cannot escape the volumes
        code, _ = self.unzip("w/evil.zip", "../outside")
        self.assertNotEqual(code, 201)
        self.assertEqual(os.listdir("outside"), [])

    def test_symlinks(self):
        self.reset()
        zi = zipfile.ZipInfo("link")
        zi.external_attr = (stat.S_IFLNK | 0o777) << 16
        mkzip("w/ln.zip", [(zi, "/etc/passwd"), ("f", b"f")])
        code, ret = self.unzip("w/ln.zip", "w/ln")
        self.assertEqual(code, 201)
        self.assertEqual(ret["nf"], 1)
        self.assertEqual(ret["skipped"], [["link", "symlink"]])
        self.assertFalse(os.path.lexists("w/ln/link"))

        mktar(
            "w/ln.tgz",
            [
                ({"name": "sym", "type": tarfile.SYMTYPE, "linkname": "/etc/passwd"}, None),
                ({"name": "hard", "type": tarfile.LNKTYPE, "linkname": "/etc/passwd"}, None),
                ({"name": "fifo", "type": tarfile.FIFOTYPE}, None),
                ({"name": "real"}, b"data"),
            ],
        )
        code, ret = self.unzip("w/ln.tgz", "w/lt")
        self.assertEqual(code, 201)
        self.assertEqual(ret["nf"], 1)
        self.assertEqual(ret["nskip"], 3)
        self.assertEqual(os.listdir("w/lt"), ["real"])

    def test_tar_formats(self):
        self.reset()
        for mode, ext in (("w", "tar"), ("w:gz", "tar.gz"), ("w:bz2", "tbz2"), ("w:xz", "txz")):
            fn = "w/t." + ext
            mktar(fn, [({"name": "d/f.txt", "mtime": 1e9}, ext.encode("ascii"))], mode)
            code, ret = self.unzip(fn, "w/out-" + ext)
            self.assertEqual(code, 201, ext)
            self.assertEqual(self.read("w/out-%s/d/f.txt" % (ext,)), ext.encode("ascii"))
            self.assertEqual(int(os.path.getmtime("w/out-%s/d/f.txt" % (ext,))), 1000000000)

    def test_perms(self):
        self.reset()
        mkzip("w/a.zip", [("x", b"1")])
        mkzip("r/a.zip", [("x", b"1")])

        # read-only user cannot extract anywhere
        code, _ = self.unzip("w/a.zip", "w/u3", pw="p3")
        self.assertIn(code, (401, 403))
        self.assertFalse(os.path.exists("w/u3"))

        # cannot extract into a read-only volume
        code, _ = self.unzip("w/a.zip", "r/x", pw="p1")
        self.assertIn(code, (401, 403))
        self.assertFalse(os.path.exists("r/x"))

        # but can extract from a read-only volume into a writable one
        code, ret = self.unzip("r/a.zip", "w/fromr", pw="p1")
        self.assertEqual(code, 201)
        self.assertEqual(self.read("w/fromr/x"), b"1")

        # anonymous has no access at all
        code, _ = self.unzip("w/a.zip", "w/anon", pw="nope")
        self.assertIn(code, (401, 403, 404))
        self.assertFalse(os.path.exists("w/anon"))

    def test_overwrite(self):
        self.reset()
        os.mkdir("w/o")
        with open("w/o/x", "wb") as f:
            f.write(b"orig")
        mkzip("w/a.zip", [("x", b"new"), ("y", b"y")])

        # default: existing files are skipped
        code, ret = self.unzip("w/a.zip", "w/o")
        self.assertEqual(code, 201)
        self.assertEqual(ret["skipped"], [["x", "file exists"]])
        self.assertEqual(self.read("w/o/x"), b"orig")
        self.assertEqual(self.read("w/o/y"), b"y")

        # ?replace needs delete-permission; u1 doesn't have it
        code, _ = self.unzip("w/a.zip", "w/o", pw="p1", extra="&replace")
        self.assertIn(code, (401, 403))
        self.assertEqual(self.read("w/o/x"), b"orig")

        # u2 does
        code, ret = self.unzip("w/a.zip", "w/o", pw="p2", extra="&replace")
        self.assertEqual(code, 201)
        self.assertEqual(ret["nskip"], 0)
        self.assertEqual(self.read("w/o/x"), b"new")

        # a folder in the way is never replaced
        os.mkdir("w/o2")
        os.mkdir("w/o2/x")
        code, ret = self.unzip("w/a.zip", "w/o2", pw="p2", extra="&replace")
        self.assertEqual(ret["skipped"], [["x", "a folder with that name exists"]])
        self.assertTrue(os.path.isdir("w/o2/x"))

    def test_limits(self):
        # zip: rejected up-front, before anything is written
        self.reset(unzip_maxn=3)
        mkzip("w/many.zip", [("f%d" % (n,), b"x") for n in range(4)])
        code, body = self.unzip("w/many.zip", "w/many")
        self.assertEqual(code, 400)
        self.assertIn("4 files", body)
        self.assertFalse(os.path.exists("w/many"))

        self.reset(unzip_maxs=1000)
        mkzip("w/big.zip", [("a", b"\x00" * 600), ("b", b"\x00" * 600)])
        code, body = self.unzip("w/big.zip", "w/big")
        self.assertEqual(code, 400)
        self.assertFalse(os.path.exists("w/big"))

        # tar: no index, so the limit kicks in while extracting
        self.reset(unzip_maxs=1000)
        mktar("w/big.tgz", [({"name": "a"}, b"\x00" * 600), ({"name": "b"}, b"\x00" * 600)])
        code, body = self.unzip("w/big.tgz", "w/bigt")
        self.assertEqual(code, 400)
        self.assertFalse(os.path.exists("w/bigt/b"))

        self.reset(unzip_maxn=1)
        mktar("w/many.tar", [({"name": "a"}, b"1"), ({"name": "b"}, b"2")], "w")
        code, body = self.unzip("w/many.tar", "w/mt")
        self.assertEqual(code, 400)
        self.assertFalse(os.path.exists("w/mt/b"))

    def test_bomb_lies(self):
        # an archive which lies about the uncompressed size
        self.reset(unzip_maxs=1000)
        mkzip("w/lie.zip", [("a", b"\x00" * 5000)])
        with open("w/lie.zip", "rb") as f:
            buf = bytearray(f.read())
        # patch the uncompressed-size in both the local and central headers
        for sig, ofs in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
            p = buf.find(sig)
            buf[p + ofs : p + ofs + 4] = (10).to_bytes(4, "little")
        with open("w/lie.zip", "wb") as f:
            f.write(bytes(buf))

        code, body = self.unzip("w/lie.zip", "w/lie")
        self.assertEqual(code, 400)
        sz = os.path.getsize("w/lie/a") if os.path.exists("w/lie/a") else 0
        self.assertLessEqual(sz, 1000)

    def test_bad_input(self):
        self.reset()
        with open("w/not.zip", "wb") as f:
            f.write(b"this is not an archive")
        code, body = self.unzip("w/not.zip", "w/x")
        self.assertEqual(code, 415)
        self.assertFalse(os.path.exists("w/x"))

        code, body = self.unzip("w", "w/x")
        self.assertEqual(code, 400)

        # truncated / corrupt
        mkzip("w/ok.zip", [("a", os.urandom(50000))])
        with open("w/ok.zip", "rb") as f:
            buf = bytearray(f.read())
        buf[100:200] = b"\x00" * 100
        with open("w/bad.zip", "wb") as f:
            f.write(bytes(buf))
        code, body = self.unzip("w/bad.zip", "w/bad")
        self.assertEqual(code, 400)
        self.assertEqual(os.listdir("w/bad") if os.path.exists("w/bad") else [], [])

        # encrypted members are skipped, not extracted as garbage
        # (zipfile can't write encrypted files; set the flag manually)
        mkzip("w/enc.zip", [("secret", b"x"), ("plain", b"p")])
        with open("w/enc.zip", "rb") as f:
            buf = bytearray(f.read())
        for sig, ofs in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            p = buf.find(sig)
            buf[p + ofs] |= 1
        with open("w/enc.zip", "wb") as f:
            f.write(bytes(buf))
        code, ret = self.unzip("w/enc.zip", "w/enc")
        self.assertEqual(code, 201)
        self.assertEqual(ret["skipped"], [["secret", "encrypted"]])

    def test_disabled(self):
        self.reset(no_unzip=True)
        mkzip("w/a.zip", [("x", b"1")])
        code, _ = self.unzip("w/a.zip", "w/a")
        self.assertEqual(code, 403)
        self.assertFalse(os.path.exists("w/a"))


if __name__ == "__main__":
    unittest.main()
