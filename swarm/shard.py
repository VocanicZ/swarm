#!/usr/bin/env python3
"""HTCondor job executable for one shard: run the project, checkpoint via exit 85.

Transferred alone to execute machines -- stdlib only, no swarm imports.
Exit codes: 0 done, 85 checkpoint, 86 machine unfit, else the run's own code.
"""
import fcntl
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path

KILL_GRACE = 10
GPU_STALE_S = 10  # governor rewrites gpu.json every 2 s; older = governor dead
POLL = float(os.environ.get("SWARM_POLL_SECONDS", 5))
MARKER_ROOT = Path("/var/tmp")
# HTCondor gives a job no PATH; sh then searches its built-in one without exporting it, and g++
# cannot find cc1plus. Every command run/setup/check starts gets this one exported.
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def sh(cmd, cwd):
    return subprocess.run(["sh", "-c", cmd], cwd=cwd).returncode == 0


def marker_dir():
    """Our private setup-marker dir, or None if it can't be trusted (foreign/shared/odd)."""
    d = MARKER_ROOT / f"swarm-setup-{os.getuid()}"
    try:
        d.mkdir(mode=0o700, exist_ok=True)
        st = os.lstat(d)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        return None
    return d


def fit(job, work):
    """check; on failure run setup once per version (flock'd across slots), then re-check."""
    check, setup = job.get("check"), job.get("setup")
    if not check or sh(check, work):
        return True
    if setup:
        version = str(job.get("version", ""))
        d = marker_dir() if re.fullmatch(r"[0-9a-f]{16}", version) else None
        if d is None:
            sh(setup, work)  # untrusted marker store: always set up, never mark
        else:
            fd = os.open(d / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)  # another slot may be mid-setup
                if not (d / version).exists() and sh(setup, work):
                    (d / version).touch()
            finally:
                os.close(fd)
    return sh(check, work)


def gpu_ok(job, run_dir):
    p = run_dir / "gpu.json"
    try:
        g = json.loads(p.read_text())
        if time.time() - p.stat().st_mtime > GPU_STALE_S:
            return False
    except (OSError, ValueError):
        return False
    return g.get("ok") is True and g.get("free_vram_mb", 0) >= job.get("vram_mb", 0)


def take_gpu(job, run_dir):
    """Locked fd on gpu0.lock if the GPU may be used now, else None."""
    if job["gpu"] == "no" or not gpu_ok(job, run_dir):
        return None
    try:
        fd = os.open(run_dir / "gpu0.lock", os.O_RDWR)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def poll_s(gpu):
    """GPU runs poll at 1 s so a revoke frees VRAM within ~3 s (2 s governor tick + 1 s)."""
    return min(POLL, 1.0) if gpu else POLL


def newest_mtime(out):
    m = out.stat().st_mtime
    for root, dirs, files in os.walk(out):
        for name in dirs + files:
            try:
                m = max(m, os.lstat(os.path.join(root, name)).st_mtime)
            except FileNotFoundError:
                pass
    return m


def kill(p, grace=KILL_GRACE):
    """SIGTERM the run's process group; SIGKILL whatever is left of it after `grace`.
    grace=0 SIGKILLs at once.

    Waits on the whole group, not just the leader: `sh -c` may die at once while
    the real work below it is still finishing its save.
    """
    deadline = time.monotonic() + grace
    try:
        if grace:
            os.killpg(p.pid, signal.SIGTERM)
        while time.monotonic() < deadline:
            p.poll()  # reap the leader so a zombie doesn't keep the group alive
            os.killpg(p.pid, 0)
            time.sleep(0.1)
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # group is gone
    p.wait()


def main(argv):
    os.environ.setdefault("PATH", DEFAULT_PATH)
    shard = argv[0]
    job = json.loads(Path("job.json").read_text())
    work, out = Path("work").resolve(), Path("out").resolve()
    if not (work / ".swarm-extracted").exists():
        work.mkdir(exist_ok=True)
        with tarfile.open("workdir.tar.gz") as t:
            t.extractall(work, filter="data")
        (work / ".swarm-extracted").touch()
    out.mkdir(exist_ok=True)
    if not fit(job, work):
        return 86

    run_dir = Path(os.environ.get("SWARM_RUN_DIR", "/run/swarm"))
    cmd = job["run"].replace("{shard}", shlex.quote(shard)).replace("{out}", shlex.quote(str(out)))
    save = job["save_seconds"]
    save_at = None
    while True:
        lock = take_gpu(job, run_dir)
        while lock is None and job["gpu"] == "required":
            time.sleep(POLL)
            lock = take_gpu(job, run_dir)
        gpu = lock is not None
        env = {**os.environ, "SWARM_GPU": "1" if gpu else "0",
               "CUDA_VISIBLE_DEVICES": "0" if gpu else ""}
        p = subprocess.Popen(["sh", "-c", cmd], cwd=work, env=env, start_new_session=True)
        if save_at is None:  # save clock runs from the first start, across GPU restarts
            save_at = time.time() + save
        try:
            while True:
                time.sleep(poll_s(gpu))
                if p.poll() is not None:
                    rc = p.returncode
                    return rc if rc >= 0 else 128 - rc
                if gpu and not gpu_ok(job, run_dir):
                    kill(p, grace=0)  # free VRAM immediately
                    break  # back to GPU selection; ./out is kept
                now = time.time()
                if now >= save_at and (newest_mtime(out) > save_at or now >= save_at + save * 0.5):
                    kill(p)
                    return 85
        finally:
            if p.returncode is None:  # unexpected exit path: don't leave the run behind
                kill(p, grace=0)
            if lock is not None:
                os.close(lock)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
