# coding: utf-8
from __future__ import division, print_function, unicode_literals

import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess as sp
import threading
import time

from .__init__ import ANYWIN, TYPE_CHECKING
from .bos import bos
from .util import Daemon, Pebkac, fsenc, min_ex, runcmd

if True:  # pylint: disable=using-constant-test
    from typing import Any, Optional

if TYPE_CHECKING:
    from .svchub import SvcHub


# placeholders which can appear inside a --wcmd template
WCMD_VARS = ("dir", "src", "dst", "arg")
RE_VAR = re.compile(r"\{([a-z]+)\}")
RE_NAME = re.compile(r"^[a-zA-Z0-9_.-]{1,32}$")
RE_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")

# runs the command, tees its output into a logfile, and records
# the exitcode; the command and logfile are passed as arguments
# (never interpolated) so no shell-injection is possible
SH_WRAP = '{ "$@"; echo $? > "$CPP_WCMD_LOG.rc"; } 2>&1 | tee -a "$CPP_WCMD_LOG"'


class WCmdDef(object):
    def __init__(self, name: str, argv: list[str]) -> None:
        self.name = name
        self.argv = argv
        self.need = sorted(set(RE_VAR.findall(" ".join(argv))))


def parse_wcmd(specs: list[str]) -> dict[str, WCmdDef]:
    """parse --wcmd NAME=COMMAND; raises ValueError on bad config"""
    ret: dict[str, WCmdDef] = {}
    for spec in specs or []:
        if "=" not in spec:
            raise ValueError("--wcmd %r must be NAME=COMMAND" % (spec,))

        name, cmd = spec.split("=", 1)
        name = name.strip()
        if not RE_NAME.match(name):
            raise ValueError("--wcmd name %r must be [a-zA-Z0-9_.-]{1,32}" % (name,))
        if name in ret:
            raise ValueError("--wcmd %r defined twice" % (name,))

        argv = shlex.split(cmd, posix=True)
        if not argv:
            raise ValueError("--wcmd %r has no command" % (name,))
        if RE_VAR.search(argv[0]):
            raise ValueError("--wcmd %r: the program name cannot be a {variable}" % (name,))

        for var in RE_VAR.findall(cmd):
            if var not in WCMD_VARS:
                t = "--wcmd %r: unknown variable {%s}; must be one of %s"
                raise ValueError(t % (name, var, ", ".join(WCMD_VARS)))

        ret[name] = WCmdDef(name, argv)

    return ret


def check_arg(arg: str) -> None:
    """validate the free-text {arg} provided by the user"""
    if not arg:
        raise Pebkac(400, "this command needs an argument")
    if len(arg) > 4096:
        raise Pebkac(400, "argument too long")
    if arg.startswith("-"):
        # option-injection; would let the user add arbitrary flags
        raise Pebkac(400, "argument cannot start with a dash")
    if re.search(r"[\x00-\x1f\x7f]", arg):
        raise Pebkac(400, "argument cannot contain control characters")


def expand(cdef: WCmdDef, vals: dict[str, str]) -> list[str]:
    """substitute placeholders; each argv element stays one element"""

    def sub(m: Any) -> str:
        return vals[m.group(1)]

    return [RE_VAR.sub(sub, x) for x in cdef.argv]


def tmux_ok(log: Any) -> str:
    """returns the tmux binary if it exists and is new enough"""
    if ANYWIN:
        return ""

    try:
        zs = shutil.which("tmux")
    except:
        zs = ""  # py2
    if not zs:
        return ""

    try:
        rc, so, _ = runcmd([zs, "-V"], timeout=5)
        m = re.search(r"([0-9]+)\.([0-9]+)", so)
        ver = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    except:
        log("could not get tmux version: " + min_ex(), 3)
        return ""

    # tmux 3.0+ runs multi-arg commands with execvp; older
    # versions would join them into a shell-string (unsafe)
    if ver < (3, 0):
        log("tmux %d.%d is too old for --wcmd (need 3.0+); not using it" % ver, 3)
        return ""

    return zs


def kill_popen(proc: sp.Popen) -> None:
    """kill a job and everything it spawned"""
    try:
        if ANYWIN:
            # taskkill /T includes child processes
            runcmd(["taskkill", "/T", "/F", "/PID", str(proc.pid)], timeout=10)
            return

        # the job is a session/pgroup leader (setsid); kill the whole group
        os.killpg(proc.pid, signal.SIGTERM)
        for _ in range(30):
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        os.killpg(proc.pid, signal.SIGKILL)
    except:
        try:
            proc.kill()
        except:
            pass


class WCmdJob(object):
    def __init__(self, jid: str, name: str, argv: list[str], cwd: str) -> None:
        self.id = jid
        self.name = name
        self.argv = argv
        self.cwd = cwd
        self.vp = ""  # vpath of the folder where it was started
        self.usr = ""
        self.ip = ""
        self.t0 = time.time()
        self.t1 = 0.0
        self.rc: Optional[int] = None
        self.mode = ""  # tmux / popen
        self.sess = ""  # tmux session name
        self.proc: Optional[sp.Popen] = None
        self.log_ap = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "argv": self.argv,
            "vp": self.vp,
            "usr": self.usr,
            "ip": self.ip,
            "t0": self.t0,
            "t1": self.t1,
            "rc": self.rc,
            "mode": self.mode,
            "sess": self.sess,
            "running": self.t1 == 0,
        }


class WCmd(object):
    """runs admin-defined commands (--wcmd) on behalf of web-ui users"""

    def __init__(self, hub: "SvcHub") -> None:
        self.hub = hub
        self.args = hub.args
        self.log_func = hub.log
        self.mutex = threading.Lock()
        self.jobs: dict[str, WCmdJob] = {}
        self.cmds = parse_wcmd(self.args.wcmd)
        self.tmux = ""
        self.dir = ""
        self.stopping = False

        if not self.cmds:
            return

        self.dir = self.args.wcmd_dir or os.path.join(self.args.E.cfg, "wcmd")
        bos.makedirs(self.dir)

        want = self.args.wcmd_tmux
        if want != "n":
            self.tmux = tmux_ok(self.log)
            if want == "y" and not self.tmux:
                raise Exception("--wcmd-tmux=y but tmux 3.0+ is not available")

        t = "web-commands enabled: %s; running jobs with %s"
        self.log(t % (", ".join(self.cmds), "tmux" if self.tmux else "popen"))
        self._load()
        Daemon(self._poller, "wcmd-poll")

    def log(self, msg: str, c: Any = 0) -> None:
        self.log_func("wcmd", msg, c)

    def status(self) -> dict[str, Any]:
        # (returns a dict since the broker can't return lists)
        with self.mutex:
            jobs = list(self.jobs.values())
        jobs.sort(key=lambda x: -x.t0)
        cmds = [
            {"name": x.name, "argv": x.argv, "need": x.need}
            for x in self.cmds.values()
        ]
        return {"cmds": cmds, "jobs": [x.to_dict() for x in jobs]}

    def run(
        self, name: str, vals: dict[str, str], cwd: str, vp: str, usr: str, ip: str
    ) -> dict[str, Any]:
        """paths in vals have already been permission-checked by httpcli"""
        cdef = self.cmds.get(name)
        if not cdef:
            raise Pebkac(404, "no such command")

        for k in cdef.need:
            if not vals.get(k):
                raise Pebkac(400, "missing value for {%s}" % (k,))

        if "arg" in cdef.need:
            check_arg(vals["arg"])

        argv = expand(cdef, vals)
        with self.mutex:
            nrun = len([x for x in self.jobs.values() if not x.t1])
            if nrun >= self.args.wcmd_maxj:
                t = "too many running jobs (%d); wait for some to finish"
                raise Pebkac(429, t % (nrun,))

            jid = "%x%04x" % (int(time.time()), random.randint(0, 0xFFFF))
            job = WCmdJob(jid, name, argv, cwd)
            job.vp = vp
            job.usr = usr
            job.ip = ip
            job.log_ap = os.path.join(self.dir, jid + ".log")
            self.jobs[jid] = job

        t = "job %s [%s] by %s @ %s in %r: %r"
        self.log(t % (jid, name, usr, ip, cwd, argv))

        try:
            if self.tmux:
                self._run_tmux(job)
            else:
                self._run_popen(job)
        except Exception as ex:
            with self.mutex:
                self.jobs.pop(jid, None)
            self.log("job %s failed to start: %s" % (jid, min_ex()), 1)
            raise Pebkac(500, "failed to start command: %r" % (ex,))

        self._save(job)
        self._gc()
        return job.to_dict()

    def _run_tmux(self, job: WCmdJob) -> None:
        job.mode = "tmux"
        job.sess = "cpp-" + job.id
        with open(job.log_ap, "wb"):
            pass

        argv = [
            self.tmux,
            "new-session",
            "-d",
            "-s",
            job.sess,
            "-c",
            job.cwd,
            "-e",
            "CPP_WCMD_LOG=" + job.log_ap,
            "sh",
            "-c",
            SH_WRAP,
            "cpp-wcmd",
        ] + job.argv
        rc, _, se = runcmd(argv, timeout=10)
        if rc:
            raise Exception("tmux returned %s: %s" % (rc, se.strip()))

    def _run_popen(self, job: WCmdJob) -> None:
        job.mode = "popen"
        ka: dict[str, Any] = {}
        if ANYWIN:
            ka["creationflags"] = getattr(sp, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            ka["preexec_fn"] = os.setsid

        logf = open(job.log_ap, "wb")
        try:
            job.proc = sp.Popen(
                [fsenc(x) for x in job.argv] if not ANYWIN else job.argv,
                cwd=fsenc(job.cwd) if not ANYWIN else job.cwd,
                stdin=sp.PIPE,
                stdout=logf,
                stderr=sp.STDOUT,
                **ka
            )
            assert job.proc.stdin  # !rm
            job.proc.stdin.close()
        finally:
            logf.close()

        Daemon(self._wait_popen, "wcmd-" + job.id, (job,))

    def _wait_popen(self, job: WCmdJob) -> None:
        assert job.proc  # !rm
        rc = job.proc.wait()
        self._finish(job, rc)

    def _finish(self, job: WCmdJob, rc: Optional[int]) -> None:
        with self.mutex:
            if job.t1:
                return
            job.rc = rc
            job.t1 = time.time()
            job.proc = None
        self.log("job %s [%s] finished; rc=%s" % (job.id, job.name, rc))
        self._save(job)

    def _poller(self) -> None:
        """tmux jobs have no process handle; check them periodically"""
        while not self.stopping:
            time.sleep(2)
            with self.mutex:
                jobs = [x for x in self.jobs.values() if not x.t1 and x.sess]
            for job in jobs:
                try:
                    self._poll_tmux(job)
                except:
                    self.log("poll %s failed: %s" % (job.id, min_ex()), 3)

    def _poll_tmux(self, job: WCmdJob) -> None:
        rc_ap = job.log_ap + ".rc"
        if bos.path.exists(rc_ap):
            try:
                with open(rc_ap, "rb") as f:
                    rc: Optional[int] = int(f.read().strip() or b"-1")
            except:
                rc = -1
            self._finish(job, rc)
            return

        if not self.tmux:
            return

        # session gone without an rc-file; killed or crashed
        rc2, _, _ = runcmd([self.tmux, "has-session", "-t", "=" + job.sess], timeout=5)
        if rc2:
            self._finish(job, None)

    def kill(self, jid: str) -> str:
        with self.mutex:
            job = self.jobs.get(jid)
        if not job:
            raise Pebkac(404, "no such job")
        if job.t1:
            return "already finished"

        self.log("killing job %s [%s]" % (job.id, job.name))
        if job.sess and self.tmux:
            runcmd([self.tmux, "kill-session", "-t", "=" + job.sess], timeout=5)
            self._finish(job, None)
        elif job.proc:
            kill_popen(job.proc)
        return "killed"

    def get_job(self, jid: str) -> dict[str, Any]:
        with self.mutex:
            job = self.jobs.get(jid)
        if not job:
            raise Pebkac(404, "no such job")
        return job.to_dict()

    def tail(self, jid: str, nbytes: int = 64 * 1024) -> str:
        with self.mutex:
            job = self.jobs.get(jid)
        if not job:
            raise Pebkac(404, "no such job")

        try:
            with open(job.log_ap, "rb") as f:
                f.seek(0, os.SEEK_END)
                sz = f.tell()
                f.seek(max(0, sz - nbytes))
                buf = f.read()
        except Exception as ex:
            return "(no output yet: %r)" % (ex,)

        txt = buf.decode("utf-8", "replace")
        txt = RE_ANSI.sub("", txt)
        # progress-bars redraw with \r; only keep the final state of each line
        lines = [x.rstrip("\r").split("\r")[-1] for x in txt.split("\n")]
        return "\n".join(lines)

    def _save(self, job: WCmdJob) -> None:
        try:
            ap = os.path.join(self.dir, job.id + ".json")
            with open(ap, "wb") as f:
                f.write(json.dumps(job.to_dict()).encode("utf-8"))
        except:
            self.log("failed to save job %s: %s" % (job.id, min_ex()), 3)

    def _load(self) -> None:
        """rediscover jobs from previous runs; tmux sessions survive restarts"""
        try:
            fns = [x for x in os.listdir(self.dir) if x.endswith(".json")]
        except:
            return

        for fn in fns:
            try:
                with open(os.path.join(self.dir, fn), "rb") as f:
                    jd = json.loads(f.read().decode("utf-8"))
                job = WCmdJob(jd["id"], jd["name"], jd["argv"], "")
                for k in ("vp", "usr", "ip", "t0", "t1", "rc", "mode", "sess"):
                    setattr(job, k, jd[k])
                job.log_ap = os.path.join(self.dir, job.id + ".log")
                if not job.t1 and job.mode != "tmux":
                    # a popen job cannot outlive copyparty
                    job.t1 = time.time()
                self.jobs[job.id] = job
            except:
                self.log("failed to load %r: %s" % (fn, min_ex()), 3)

        self._gc()

    def _gc(self) -> None:
        """forget the oldest finished jobs"""
        with self.mutex:
            done = [x for x in self.jobs.values() if x.t1]
            done.sort(key=lambda x: x.t0)
            drop = done[: max(0, len(done) - self.args.wcmd_nkeep)]
            for job in drop:
                self.jobs.pop(job.id, None)

        for job in drop:
            for sfx in (".json", ".log", ".log.rc"):
                try:
                    os.unlink(os.path.join(self.dir, job.id + sfx))
                except:
                    pass

    def shutdown(self) -> None:
        # tmux jobs keep running (that's the point); popen jobs die with us
        self.stopping = True
        with self.mutex:
            jobs = [x for x in self.jobs.values() if x.proc]
        for job in jobs:
            assert job.proc  # !rm
            kill_popen(job.proc)
