#!/usr/bin/env python3
# coding: utf-8
from __future__ import division, print_function, unicode_literals

import json
import os
import shlex
import shutil
import sys
import tempfile
import time
import unittest

from copyparty.__init__ import ANYWIN
from copyparty.authsrv import AuthSrv
from copyparty.httpcli import HttpCli
from copyparty.util import Pebkac, runcmd
from copyparty.wcmd import check_arg, expand, parse_wcmd, tmux_ok
from tests import util as tu
from tests.util import Cfg

PY = shlex.quote(sys.executable.replace("\\", "/"))

# prints its cwd and arguments, one per line
ECHO = PY + " -c 'import os,sys; print(\"cwd=\"+os.getcwd()); [print(\"arg=\"+x) for x in sys.argv[1:]]'"
SLEEP = PY + " -c 'import time; time.sleep(60)'"
FAIL = PY + " -c 'import sys; sys.exit(3)'"


class TestWcmdParse(unittest.TestCase):
    def test_parse(self):
        cmds = parse_wcmd(["dl=aria2c --dir={dir} -- {arg}", "x7=7z x -o{dst} -- {src}"])
        self.assertEqual(cmds["dl"].argv, ["aria2c", "--dir={dir}", "--", "{arg}"])
        self.assertEqual(cmds["dl"].need, ["arg", "dir"])
        self.assertEqual(cmds["x7"].need, ["dst", "src"])

        bad = [
            "noequals",
            "=cmd",
            "bad name=cmd",
            "x=",
            "x={arg} foo",  # program name cannot be a variable
            "x=echo {nope}",
        ]
        for spec in bad:
            with self.assertRaises(ValueError, msg=spec):
                parse_wcmd([spec])

        with self.assertRaises(ValueError):
            parse_wcmd(["a=echo", "a=echo"])

    def test_expand_no_injection(self):
        cdef = parse_wcmd(["e=echo -- {arg}"])["e"]
        evil = "x; rm -rf / $(id) `id` && echo 'hi' \"q\""
        argv = expand(cdef, {"arg": evil})
        self.assertEqual(argv, ["echo", "--", evil])

        cdef = parse_wcmd(["e=7z x -o{dst} {src}"])["e"]
        argv = expand(cdef, {"dst": "/a b/c", "src": "/x {arg} y"})
        self.assertEqual(argv, ["7z", "x", "-o/a b/c", "/x {arg} y"])

    def test_check_arg(self):
        check_arg("https://example.com/a?b=c&d")
        for bad in ("", "-o/etc/x", "--help", "a\nb", "a\x00b", "a\rb", "x" * 5000):
            with self.assertRaises(Pebkac, msg=repr(bad)):
                check_arg(bad)


class TestWcmd(unittest.TestCase):
    def setUp(self):
        self.td = tu.get_ramdisk()
        self.conn = None

    def tearDown(self):
        if self.conn:
            self.conn.shutdown()
        os.chdir(tempfile.gettempdir())
        shutil.rmtree(self.td, ignore_errors=True)

    def log(self, src, msg, c=0):
        print(("[%s] %s" % (src, msg)).encode("ascii", "replace").decode("ascii"))

    def reset(self, cmds, **ka):
        if self.conn:
            self.conn.shutdown()
            self.conn = None
        os.chdir(self.td)
        td = os.path.join(self.td, "vfs")
        if os.path.exists(td):
            shutil.rmtree(td)
        os.mkdir(td)
        os.chdir(td)
        for d in ("a", "b", "r"):
            os.mkdir(d)
        with open("a/f.txt", "wb") as f:
            f.write(b"f")
        with open("r/secret.txt", "wb") as f:
            f.write(b"s")

        # u1 = admin in a, u2 = admin in b, u3 = read-write in a (no admin)
        vols = ["a:a:A,u1:rw,u3", "b:b:A,u2", "r:r:r,u2"]
        wdir = os.path.join(self.td, "wcmd")
        ka.setdefault("wcmd_tmux", "n")
        self.args = Cfg(v=vols, a=["u1:p1", "u2:p2", "u3:p3"], wcmd=cmds, wcmd_dir=wdir, **ka)
        self.asrv = AuthSrv(self.args, self.log)
        self.conn = tu.VHttpConn(self.args, self.asrv, self.log, b"", True)
        self.wcmd = self.conn.hsrv.hub.wcmd

    def req(self, method, url, pw, body=None):
        hdr = ["%s /%s HTTP/1.1" % (method, url), "PW: " + pw, "Connection: close"]
        buf = b""
        if body is not None:
            buf = json.dumps(body).encode("utf-8")
            hdr.append("Content-Type: application/json")
        hdr.append("Content-Length: %d" % (len(buf),))
        raw = ("\r\n".join(hdr) + "\r\n\r\n").encode("utf-8") + buf
        HttpCli(self.conn.setbuf(raw)).run()
        h, b = self.conn.s._reply.decode("utf-8").split("\r\n\r\n", 1)
        code = int(h.split(" ")[1])
        print("%s %s --> %d %s" % (method, url, code, b[:300]))
        return code, b

    def run_cmd(self, vp, pw, **body):
        body["act"] = "run"
        code, b = self.req("POST", vp + "?wcmd", pw, body)
        return code, (json.loads(b) if code == 201 else b)

    def wait(self, jid, tmax=20):
        t0 = time.time()
        while time.time() - t0 < tmax:
            job = self.wcmd.get_job(jid)
            if not job["running"]:
                return job
            if job["sess"]:
                self.wcmd._poll_tmux(self.wcmd.jobs[jid])
            time.sleep(0.1)
        raise Exception("job did not finish")

    def test_run(self):
        self.reset(["echo=" + ECHO + " {arg} {dir}", "fail=" + FAIL])

        code, b = self.req("GET", "a/?wcmd", "p1")
        self.assertEqual(code, 200)
        st = json.loads(b)
        self.assertEqual([x["name"] for x in st["cmds"]], ["echo", "fail"])
        self.assertEqual(st["jobs"], [])

        evil = "x; touch pwned $(touch pwned2)"
        code, job = self.run_cmd("a/", "p1", cmd="echo", arg=evil)
        self.assertEqual(code, 201)
        job = self.wait(job["id"])
        self.assertEqual(job["rc"], 0)
        self.assertEqual(job["usr"], "u1")

        code, txt = self.req("GET", "a/?wcmd=log&job=" + job["id"], "p1")
        self.assertEqual(code, 200)
        lines = txt.strip().split("\n")
        cwd = os.path.realpath(os.path.join(self.td, "vfs", "a"))
        self.assertEqual(os.path.normcase(lines[0][4:]), os.path.normcase(cwd))
        self.assertEqual(lines[1], "arg=" + evil)
        self.assertEqual(os.path.normcase(lines[2][4:]), os.path.normcase(cwd))
        self.assertFalse(os.path.exists("a/pwned"))
        self.assertFalse(os.path.exists("a/pwned2"))

        code, job = self.run_cmd("a/", "p1", cmd="fail")
        self.assertEqual(code, 201)
        self.assertEqual(self.wait(job["id"])["rc"], 3)

        # option-injection is refused
        code, _ = self.run_cmd("a/", "p1", cmd="echo", arg="--version")
        self.assertEqual(code, 400)
        code, _ = self.run_cmd("a/", "p1", cmd="echo", arg="")
        self.assertEqual(code, 400)
        code, _ = self.run_cmd("a/", "p1", cmd="nope", arg="x")
        self.assertEqual(code, 404)

    def test_perms(self):
        self.reset(["echo=" + ECHO + " {src}", "out=" + ECHO + " {dst}"])

        # not admin
        code, _ = self.req("GET", "a/?wcmd", "p3")
        self.assertEqual(code, 403)
        code, _ = self.run_cmd("a/", "p3", cmd="echo", src="a/f.txt")
        self.assertEqual(code, 403)

        # admin in a, but not in b
        code, _ = self.run_cmd("b/", "p1", cmd="echo", src="a/f.txt")
        self.assertIn(code, (401, 403, 404))

        # {src} needs read-access; u1 cannot read r/
        code, _ = self.run_cmd("a/", "p1", cmd="echo", src="r/secret.txt")
        self.assertIn(code, (401, 403, 404))
        code, _ = self.run_cmd("a/", "p1", cmd="echo", src="a/../r/secret.txt")
        self.assertIn(code, (400, 401, 403, 404))

        # {dst} needs write-access; u2 can read r/ but not write
        code, _ = self.run_cmd("b/", "p2", cmd="out", dst="r/x")
        self.assertIn(code, (401, 403))
        self.assertFalse(os.path.exists("r/x"))

        code, job = self.run_cmd("a/", "p1", cmd="echo", src="/a/f.txt")
        self.assertEqual(code, 201)
        self.wait(job["id"])
        code, txt = self.req("GET", "a/?wcmd=log&job=" + job["id"], "p1")
        self.assertTrue(txt.strip().endswith("f.txt"), txt)

        code, job2 = self.run_cmd("a/", "p1", cmd="out", dst="a/new/sub")
        self.assertEqual(code, 201)
        self.assertTrue(os.path.isdir("a/new/sub"))

        # u2 (admin in b) cannot see or touch u1's jobs from a
        code, b = self.req("GET", "b/?wcmd", "p2")
        self.assertEqual(json.loads(b)["jobs"], [])
        code, _ = self.req("GET", "b/?wcmd=log&job=" + job["id"], "p2")
        self.assertEqual(code, 404)
        code, _ = self.req("POST", "b/?wcmd", "p2", {"act": "kill", "job": job["id"]})
        self.assertEqual(code, 404)

    def test_kill_and_limits(self):
        self.reset(["sleep=" + SLEEP], wcmd_maxj=2)

        code, j1 = self.run_cmd("a/", "p1", cmd="sleep")
        code, j2 = self.run_cmd("a/", "p1", cmd="sleep")
        self.assertEqual(code, 201)
        code, _ = self.run_cmd("a/", "p1", cmd="sleep")
        self.assertEqual(code, 429)

        for j in (j1, j2):
            code, b = self.req("POST", "a/?wcmd", "p1", {"act": "kill", "job": j["id"]})
            self.assertEqual(code, 200)
            job = self.wait(j["id"])
            self.assertNotEqual(job["rc"], 0)

        code, _ = self.run_cmd("a/", "p1", cmd="sleep")
        self.assertEqual(code, 201)

    def test_csrf(self):
        self.reset(["echo=" + ECHO + " {arg}"], acao=["*"], acam=["GET", "HEAD"], acao_re=None, allow_csrf=False)
        # a cross-site form-post (text/plain, no preflight) must be rejected
        body = '{"act": "run", "cmd": "echo", "arg": "pwned"}'
        hdr = [
            "POST /a/?wcmd HTTP/1.1",
            "Cookie: cppwd=p1",
            "Origin: http://evil.example.com",
            "Content-Type: text/plain",
            "Content-Length: %d" % (len(body),),
            "Connection: close",
        ]
        raw = ("\r\n".join(hdr) + "\r\n\r\n" + body).encode("utf-8")
        HttpCli(self.conn.setbuf(raw)).run()
        h = self.conn.s._reply.decode("utf-8").split("\r\n")[0]
        self.assertIn(" 403 ", h)
        self.assertEqual(self.wcmd.jobs, {})

    def test_disabled(self):
        self.reset([])
        code, _ = self.req("GET", "a/?wcmd", "p1")
        self.assertEqual(code, 404)

    @unittest.skipIf(ANYWIN or not tmux_ok(print), "needs tmux 3.0+")
    def test_tmux(self):
        self.reset(["echo=" + ECHO + " {arg}", "sleep=" + SLEEP], wcmd_tmux="y")
        self.assertTrue(self.wcmd.tmux)

        evil = "a'b\"c $(touch pwned) ; touch pwned2"
        code, job = self.run_cmd("a/", "p1", cmd="echo", arg=evil)
        self.assertEqual(code, 201)
        self.assertEqual(job["mode"], "tmux")
        job = self.wait(job["id"])
        self.assertEqual(job["rc"], 0)
        code, txt = self.req("GET", "a/?wcmd=log&job=" + job["id"], "p1")
        self.assertIn("arg=" + evil, txt)
        self.assertFalse(os.path.exists("a/pwned"))
        self.assertFalse(os.path.exists("a/pwned2"))

        code, job = self.run_cmd("a/", "p1", cmd="sleep")
        sess = job["sess"]
        rc, _, _ = runcmd(["tmux", "has-session", "-t", "=" + sess])
        self.assertEqual(rc, 0)

        # a restart rediscovers the still-running tmux job
        self.conn.hsrv.hub.wcmd.shutdown()
        from copyparty.wcmd import WCmd

        w2 = WCmd(self.conn.hsrv.hub)
        self.assertTrue(w2.get_job(job["id"])["running"])
        w2.kill(job["id"])
        w2.shutdown()
        rc, _, _ = runcmd(["tmux", "has-session", "-t", "=" + sess])
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
