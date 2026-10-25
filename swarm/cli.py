import argparse
import os
import subprocess
import sys
from pathlib import Path

from swarm.jobspec import SpecError

ROOT = Path.home() / ".swarm/jobs"


def summarize(name, total, done, rows):
    running = [r for r in rows if r[1] == 2]
    idle = sum(1 for r in rows if r[1] == 1)
    hosts = sorted({r[2].split("@")[-1] for r in running})
    line = f"{name}: {done}/{total} done, {len(running)} running"
    if hosts:
        line += f" ({', '.join(hosts)})"
    return f"{line}, {idle} idle"


def _q(*args):
    r = subprocess.run(["condor_q", "-af", *args], capture_output=True, text=True)
    return [l.split() for l in r.stdout.splitlines() if l.strip()]


def shard_rows(lines):
    # DAGMan and the local merge job share JobBatchName but have no SwarmJob
    return [(r[0], int(r[1]), r[2]) for r in lines
            if len(r) >= 4 and r[1].isdigit() and r[3] != "undefined"]


def status(name=None):
    rows = shard_rows(_q("JobBatchName", "JobStatus", "RemoteHost", "SwarmJob"))
    names = [name] if name else sorted(p.name for p in ROOT.glob("*") if p.is_dir())
    for n in names:
        d = ROOT / n
        total = len((d / "shards.txt").read_text().split()) if (d / "shards.txt").exists() else 0
        done = len(list(d.glob("shards/*/out")))
        print(summarize(n, total, done, [r for r in rows if r[0] == n]))
        log = d / "merge.log"
        merged = log.exists() and "Job terminated" in log.read_text()
        print(f"  merge: {'done' if merged else 'pending'}")


def _install_sh():
    p = Path("/opt/swarm/install.sh")
    return str(p if p.parent.exists() else Path(__file__).resolve().parent.parent / "install.sh")


def main(argv):
    ap = argparse.ArgumentParser(prog="swarm")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("submit").add_argument("jobdir")
    sub.add_parser("status").add_argument("name", nargs="?")
    sub.add_parser("cancel").add_argument("name")
    sub.add_parser("workers")
    j = sub.add_parser("join")
    j.add_argument("coord_ip")
    j.add_argument("--link-mbps", type=int)
    sub.add_parser("leave")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "submit":
            from swarm import submit
            print(submit.submit(Path(a.jobdir)))
        elif a.cmd == "status":
            status(a.name)
        elif a.cmd == "cancel":
            return subprocess.run(["condor_rm", "-constraint", f'JobBatchName == "{a.name}"']).returncode
        elif a.cmd == "workers":
            return subprocess.run(["condor_status", "-af", "Machine", "State", "Cpus", "SwarmCapCores",
                                   "SwarmGpuFreeMB"]).returncode
        elif a.cmd == "join":
            args = ["sudo", _install_sh(), "worker", a.coord_ip]
            if a.link_mbps:
                args += ["--link-mbps", str(a.link_mbps)]
            os.execvp("sudo", args)
        elif a.cmd == "leave":
            return subprocess.run(["sudo", "systemctl", "disable", "--now", "swarm-governor", "condor"]).returncode
    except SpecError as e:
        print(e, file=sys.stderr)
        return 2
    return 0
