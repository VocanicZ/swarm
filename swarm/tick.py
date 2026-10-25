"""Coordinator tick (root, 1/min): invalidate dead workers' ads, publish coordinator free CPU."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from swarm.governor import CG

CONF = Path("/etc/swarm/coordinator.conf")
STATE = Path("/var/lib/swarm/tick.json")
ROOT_STAT = Path("/sys/fs/cgroup/cpu.stat")


def dead_machines(rows: list[tuple[str, int]], now: float, coord: str, max_age: float = 180) -> list[str]:
    """Machines whose every slot was last heard from more than max_age s ago."""
    newest: dict[str, float] = {}
    for m, heard in rows:
        if m != coord and isinstance(heard, (int, float)):
            newest[m] = max(newest.get(m, heard), heard)
    return sorted(m for m, h in newest.items() if now - h > max_age)


def coord_free_cores(cpu_limit: float, total_usec: int, jobs_usec: int, dt_s: float) -> float:
    return min(cpu_limit, max(0, cpu_limit - (total_usec - jobs_usec) / 1e6 / dt_s))


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


def invalidate(machine: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", machine):
        return False
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

    status = run("condor_status", "-af", "Machine", "LastHeardFrom")
    invalidated = False
    if status is not None:
        rows = []
        for line in status.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit():
                rows.append((parts[0], int(parts[1])))
        for m in dead_machines(rows, time.time(), coord):
            invalidated |= invalidate(m)

    now = time.time()
    try:
        total, jobs = usage_usec(ROOT_STAT), usage_usec(CG / "cpu.stat")
    except (OSError, ValueError) as e:
        print(f"cpu.stat: {e}", file=sys.stderr)
        total = jobs = None
    last = st.get("last")
    if total is not None and last and now > last["t"] and total >= last["total"] and jobs >= last["jobs"]:
        free = coord_free_cores(cpu_limit, total - last["total"], jobs - last["jobs"], now - last["t"])
        run("condor_qedit", "-constraint", "SwarmJob =!= undefined && JobStatus == 1",
            "SwarmCoordRate", f"{free:.1f}")
    if invalidated:
        run("condor_reschedule")

    new = {}
    if total is not None:
        new["last"] = {"t": now, "total": total, "jobs": jobs}
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(new))
        os.replace(tmp, STATE)
    except OSError as e:
        print(f"state: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
