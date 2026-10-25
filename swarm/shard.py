#!/usr/bin/env python3
"""HTCondor job executable for one shard: run the project, checkpoint via exit 85.

Transferred alone to execute machines -- stdlib only, no swarm imports.
Exit codes: 0 done, 85 checkpoint, 86 machine unfit, else the run's own code.
"""
import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

KILL_GRACE = 10
POLL = float(os.environ.get("SWARM_POLL_SECONDS", 5))
MARKER_ROOT = Path("/var/tmp")


def sh(cmd, cwd):
    return subprocess.run(["sh", "-c", cmd], cwd=cwd).returncode == 0


def fit(job, work):
    """check; on failure run setup once per version, then re-check."""
    if not job.get("check") or sh(job["check"], work):
        return True
    marker = MARKER_ROOT / f"swarm-setup-{os.getuid()}" / str(job["version"])
    if job.get("setup") and not marker.exists() and sh(job["setup"], work):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    return sh(job["check"], work)


def gpu_ok(job, run_dir):
    try:
        g = json.loads((run_dir / "gpu.json").read_text())
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


def newest_mtime(out):
    m = out.stat().st_mtime
    for root, dirs, files in os.walk(out):
        for name in dirs + files:
            try:
                m = max(m, os.lstat(os.path.join(root, name)).st_mtime)
            except FileNotFoundError:
                pass
    return m


def kill(p):
    """SIGTERM the run's process group; SIGKILL whatever is left of it after KILL_GRACE.

    Waits on the whole group, not just the leader: `sh -c` may die at once while
    the real work below it is still finishing its save.
    """
    deadline = time.monotonic() + KILL_GRACE
    try:
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
                time.sleep(POLL)
                if p.poll() is not None:
                    rc = p.returncode
                    return rc if rc >= 0 else 128 - rc
                if gpu and not gpu_ok(job, run_dir):
                    kill(p)
                    break  # back to GPU selection; ./out is kept
                now = time.time()
                if now >= save_at and (newest_mtime(out) > save_at or now >= save_at + save * 0.5):
                    kill(p)
                    return 85
        finally:
            if p.returncode is None:
                kill(p)
            if lock is not None:
                os.close(lock)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
