"""Coordinator tick (root, 1/min): invalidate dead workers' ads, publish coordinator free CPU."""
import json
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from swarm.governor import CG

CONF = Path("/etc/swarm/coordinator.conf")
STATE = Path("/var/lib/swarm/tick.json")
ROOT_STAT = Path("/sys/fs/cgroup/cpu.stat")
MAX_MISSES = 3


def parse_addr(my_address: str) -> tuple[str, int]:
    m = re.match(r"<([^:>?]+):(\d+)", my_address)
    return m.group(1), int(m.group(2))


def update_misses(misses: dict[str, int], alive: dict[str, bool]) -> tuple[dict[str, int], list[str]]:
    new, dead = {}, []
    for m, ok in alive.items():
        n = 0 if ok else misses.get(m, 0) + 1
        if n >= MAX_MISSES:
            dead.append(m)
            n = 0
        new[m] = n
    return new, dead


def coord_free_cores(cpu_limit: float, total_usec: int, jobs_usec: int, dt_s: float) -> float:
    return max(0, cpu_limit - (total_usec - jobs_usec) / 1e6 / dt_s)


def read_conf() -> dict[str, str]:
    out = {}
    for line in CONF.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def usage_usec(stat: Path) -> int:
    for line in stat.read_text().splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1])
    raise ValueError(f"no usage_usec in {stat}")


def run(*cmd: str) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"{cmd[0]}: {e}", file=sys.stderr)
        return None
    if r.returncode:
        print(f"{cmd[0]} rc={r.returncode}: {r.stderr.strip()}", file=sys.stderr)
        return None
    return r.stdout


def reachable(addr: str) -> bool:
    try:
        with socket.create_connection(parse_addr(addr), timeout=3):
            return True
    except (OSError, AttributeError):
        return False


def invalidate(machine: str) -> bool:
    ad = f'MyType = "Query"\nTargetType = "Machine"\nRequirements = TARGET.Machine == "{machine}"\n'
    with tempfile.NamedTemporaryFile("w", suffix=".ad") as f:
        f.write(ad)
        f.flush()
        return run("condor_advertise", "INVALIDATE_STARTD_ADS", f.name) is not None


def main() -> int:
    try:
        conf = read_conf()
        cpu_limit, coord = float(conf["CPU_LIMIT"]), conf["COORD_MACHINE"]
    except (OSError, KeyError, ValueError) as e:
        print(f"bad config {CONF}: {e}", file=sys.stderr)
        return 1
    try:
        st = json.loads(STATE.read_text())
    except (OSError, ValueError):
        st = {}
    misses = st.get("misses", {})

    status = run("condor_status", "-af", "Machine", "MyAddress")
    invalidated = False
    if status is not None:  # on failure keep old counts rather than guess
        alive = {}
        for line in status.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0] != coord:
                alive[parts[0]] = alive.get(parts[0], False) or reachable(parts[1])
        misses, dead = update_misses(misses, alive)
        for m in dead:
            invalidated |= invalidate(m)

    now = time.time()
    try:
        total, jobs = usage_usec(ROOT_STAT), usage_usec(CG / "cpu.stat")
    except (OSError, ValueError) as e:
        print(f"cpu.stat: {e}", file=sys.stderr)
        total = jobs = None
    last = st.get("last")
    if total is not None and last and now > last["t"]:
        free = coord_free_cores(cpu_limit, total - last["total"], jobs - last["jobs"], now - last["t"])
        run("condor_qedit", "-constraint", "SwarmJob =!= undefined && JobStatus == 1",
            "SwarmCoordRate", f"{free:.1f}")
    if invalidated:
        run("condor_reschedule")

    new = {"misses": misses}
    if total is not None:
        new["last"] = {"t": now, "total": total, "jobs": jobs}
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(new))
    except OSError as e:
        print(f"state: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
