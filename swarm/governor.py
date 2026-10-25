"""Worker governor: cap HTCondor's cgroup to 100% - owner's use - 10% per resource."""
import json
import math
import os
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

CG = Path("/sys/fs/cgroup/system.slice/condor.service/htcondor")
RUN = Path("/run/swarm")
CONF = Path("/etc/swarm/worker.conf")
TICK_S = 2
HZ = 100  # USER_HZ for /proc/stat jiffies
NO_GPU = {"ok": False, "free_vram_mb": 0}


def cap_pct(your_pct: float) -> float:
    return max(0.0, 100 - your_pct - 10)


class Smoother:
    """Min of raw values within the last window_s: shrink now, grow after calm."""

    def __init__(self, window_s: float = 30):
        self.window_s, self.buf = window_s, deque()

    def push(self, t: float, raw: float) -> float:
        self.buf.append((t, raw))
        while self.buf[0][0] < t - self.window_s:
            self.buf.popleft()
        return min(v for _, v in self.buf)


def cpu_times(stat_text: str) -> tuple[int, int]:
    f = [int(x) for x in stat_text.splitlines()[0].split()[1:]]
    total = sum(f[:8])  # user..steal; guest is already inside user
    return total - f[3] - f[4], total


def parse_nvidia(gpu_csv: str, apps_csv: str) -> dict:
    util, total, used = (int(x) for x in gpu_csv.strip().splitlines()[0].split(","))
    procs = {}
    for line in apps_csv.strip().splitlines():
        pid, mb = (int(x) for x in line.split(","))
        procs[pid] = mb
    return {"util_pct": util, "total_mb": total, "used_mb": used, "procs": procs}


def gpu_state(util_pct: float, total_mb: int, your_vram_mb: int) -> dict:
    free = max(0, int(total_mb * 0.9 - your_vram_mb))
    return {"ok": cap_pct(util_pct) > 0 and free > 0, "free_vram_mb": free}


def publish_gpu(gpu: dict | None, util_pct: float, your_vram_mb: int) -> dict:
    if gpu is None:
        return dict(NO_GPU)
    return gpu_state(util_pct, gpu["total_mb"], your_vram_mb)


def startd_ad(cap_cpu_pct: float, ncpu: int, link_mbps: float, gpus: int, gpu_free_mb: int) -> str:
    return (f"SwarmCapCores = {math.floor(cap_cpu_pct * ncpu / 100)}\n"
            f"SwarmLinkMbps = {link_mbps:g}\n"
            f"SwarmGpus = {gpus}\n"
            f"SwarmGpuFreeMB = {gpu_free_mb}\n")


def read_gpu() -> dict | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        q = ["nvidia-smi", "--format=csv,noheader,nounits"]
        gpu = subprocess.run(q + ["--query-gpu=utilization.gpu,memory.total,memory.used"],
                             capture_output=True, text=True, timeout=5, check=True).stdout
        apps = subprocess.run(q + ["--query-compute-apps=pid,used_memory"],
                              capture_output=True, text=True, timeout=5, check=True).stdout
        return parse_nvidia(gpu, apps)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


# ---- thin I/O below ----

def _link_mbps() -> float:
    try:
        for line in CONF.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "LINK_MBPS":
                return float(v.strip())
    except (OSError, ValueError):
        pass
    return 100.0


def _meminfo() -> tuple[int, int]:
    d = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, _, v = line.partition(":")
        d[k] = int(v.split()[0]) * 1024
    return d["MemTotal"], d["MemAvailable"]


def _cg_int(name: str, key: str | None = None) -> int:
    text = (CG / name).read_text()
    if key is None:
        return int(text)
    return next(int(l.split()[1]) for l in text.splitlines() if l.startswith(key + " "))


def _swarm_gpu_mb(gpu: dict) -> int:
    mb = 0
    for pid, used in gpu["procs"].items():
        try:
            if "/htcondor/" in Path(f"/proc/{pid}/cgroup").read_text():
                mb += used
        except OSError:
            pass
    return mb


def _atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _write(name: str, val) -> None:
    try:
        (CG / name).write_text(str(val))
    except OSError as e:
        print(f"governor: cannot write {name}: {e}", flush=True)


def main() -> int:
    RUN.mkdir(parents=True, exist_ok=True)
    lock = RUN / "gpu0.lock"
    os.close(os.open(lock, os.O_CREAT | os.O_RDWR, 0o666))
    os.chmod(lock, 0o666)
    ncpu = os.cpu_count() or 1
    link = _link_mbps()
    sm = {k: Smoother() for k in ("cpu", "ram", "gpu")}
    prev = None  # (busy, total, swarm_usec)
    over_since = frozen_at = None
    last_log = None
    while True:
        now = time.monotonic()
        try:
            busy, total = cpu_times(Path("/proc/stat").read_text())
            sw_usec = _cg_int("cpu.stat", "usage_usec")
            mem_total, mem_avail = _meminfo()
            mem_cur = _cg_int("memory.current")
        except (OSError, StopIteration, ValueError) as e:
            print(f"governor: measure failed: {e}", flush=True)
            time.sleep(TICK_S)
            continue
        cpu_you = 0.0
        if prev and total > prev[1]:
            sw_j = (sw_usec - prev[2]) / 1e6 * HZ
            cpu_you = max(0.0, min(100.0, ((busy - prev[0]) - sw_j) / (total - prev[1]) * 100))
        prev = (busy, total, sw_usec)
        ram_you = max(0.0, (mem_total - mem_avail - mem_cur) / mem_total * 100)

        gpu = read_gpu()
        your_vram = util_you = 0
        if gpu:
            sw_mb = _swarm_gpu_mb(gpu)
            your_vram = max(0, gpu["used_mb"] - sw_mb)
            util_you = 0 if sw_mb else gpu["util_pct"]  # ceiling: swarm GPU job hides owner util
        caps = {"cpu": sm["cpu"].push(now, cap_pct(cpu_you)),
                "ram": sm["ram"].push(now, cap_pct(ram_you)),
                "gpu": sm["gpu"].push(now, cap_pct(util_you))}

        _write("cpu.max", f"{int(max(1000, caps['cpu'] * ncpu * 1000))} 100000")
        high = int(caps["ram"] / 100 * mem_total)
        _write("memory.high", high)
        if mem_cur <= high:
            over_since = None
            if frozen_at is not None:
                _write("cgroup.freeze", 0)
                frozen_at = None
        else:
            over_since = over_since or now
            if frozen_at is None and now - over_since > 30:
                _write("cgroup.freeze", 1)
                frozen_at = now
            elif frozen_at is not None and now - frozen_at > 30:
                subprocess.run(["condor_vacate"], capture_output=True, timeout=30)  # loses work since last save
                _write("cgroup.freeze", 0)
                frozen_at = over_since = None

        pub = publish_gpu(gpu, 90 - caps["gpu"], your_vram)
        _atomic(RUN / "gpu.json", json.dumps(pub))
        _atomic(RUN / "startd.ad", startd_ad(caps["cpu"], ncpu, link, 1 if gpu else 0, pub["free_vram_mb"]))

        state = tuple(round(caps[k]) for k in ("cpu", "ram", "gpu")) + (pub["ok"], pub["free_vram_mb"])
        if state != last_log:
            print(f"governor: caps cpu={state[0]}% ram={state[1]}% gpu={state[2]}% "
                  f"gpu_ok={pub['ok']} free_vram_mb={pub['free_vram_mb']}", flush=True)
            last_log = state
        time.sleep(TICK_S)


if __name__ == "__main__":
    sys.exit(main())
