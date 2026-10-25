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


def owner_cpu(prev: tuple | None, busy: int, total: int, sw_usec: int) -> float | None:
    """Owner CPU % since prev=(busy, total, swarm_usec); None until a delta exists."""
    if not prev or total <= prev[1]:
        return None
    sw_j = max(0, sw_usec - prev[2]) / 1e6 * HZ
    return max(0.0, min(100.0, ((busy - prev[0]) - sw_j) / (total - prev[1]) * 100))


def startd_ad(cap_cpu_pct: float, ncpu: int, link_mbps: float, gpus: int, gpu_free_mb: int,
              cap_ram_pct: float, mem_total_mb: int, now: float) -> str:
    return (f"SwarmCapCores = {math.floor(cap_cpu_pct * ncpu / 100)}\n"
            f"SwarmLinkMbps = {link_mbps:g}\n"
            f"SwarmGpus = {gpus}\n"
            f"SwarmGpuFreeMB = {gpu_free_mb}\n"
            f"SwarmCapMemoryMB = {int(cap_ram_pct * mem_total_mb / 100)}\n"
            f"SwarmAdTime = {int(now)}\n")  # START refuses ads older than 60 s (dead governor)


def parse_pmon(text: str) -> dict:
    """`nvidia-smi pmon -s u` -> {pid: sm%}; '-' counts as 0."""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if not f or f[0].startswith("#") or len(f) < 4:
            continue
        out[int(f[1])] = int(f[3]) if f[3].isdigit() else 0
    return out


def mem_step(state: tuple, now: float, cur: int, high: int) -> tuple[tuple, str | None]:
    """state=(over_since, frozen_at). action in None|freeze|vacate|unfreeze (vacate implies unfreeze)."""
    over, frozen = state
    if cur <= high:
        return (None, None), ("unfreeze" if frozen is not None else None)
    over = now if over is None else over
    if frozen is None:
        return ((over, now), "freeze") if now - over > 30 else ((over, None), None)
    if now - frozen > 30:
        return (None, None), "vacate"
    return (over, frozen), None


def read_gpu() -> dict | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        q = ["nvidia-smi", "--format=csv,noheader,nounits"]
        run = lambda a: subprocess.run(a, capture_output=True, text=True, timeout=5, check=True).stdout
        gpu = parse_nvidia(run(q + ["--query-gpu=utilization.gpu,memory.total,memory.used"]),
                           run(q + ["--query-compute-apps=pid,used_memory"]))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    try:
        gpu["sm"] = parse_pmon(run(["nvidia-smi", "pmon", "-c", "1", "-s", "u"]))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        gpu["sm"] = None  # fall back to total-util attribution
    return gpu


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


def _kv(name: str, key: str) -> int:
    for l in (CG / name).read_text().splitlines():
        if l.startswith(key + " "):
            return int(l.split()[1])
    raise ValueError(f"{key} not in {name}")


def measure_cg() -> tuple[int, int, int, bool]:
    """(cpu usage_usec, memory.current, anon bytes, present). Missing cgroup -> zeros, False."""
    try:
        return (_kv("cpu.stat", "usage_usec"), int((CG / "memory.current").read_text()),
                _kv("memory.stat", "anon"), True)
    except (OSError, ValueError):
        return 0, 0, 0, False


def _swarm_pids(pids) -> set:
    out = set()
    for pid in pids:
        try:
            if "/htcondor/" in Path(f"/proc/{pid}/cgroup").read_text():
                out.add(pid)
        except OSError:
            pass
    return out


def owner_gpu(gpu: dict) -> tuple[int, float]:
    """(owner vram MB, owner util %). Util via pmon per-pid SM%, else total util unless swarm has a GPU proc."""
    sm = gpu.get("sm")
    swarm = _swarm_pids(set(gpu["procs"]) | set(sm or ()))
    vram = max(0, gpu["used_mb"] - sum(gpu["procs"].get(p, 0) for p in swarm))
    if sm is not None:
        return vram, max(0, gpu["util_pct"] - sum(sm.get(p, 0) for p in swarm))
    return vram, 0 if swarm else gpu["util_pct"]


def _atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


_logged = set()


def _log_once(key: str, msg: str) -> None:
    if key not in _logged:
        _logged.add(key)
        print(f"governor: {msg}", flush=True)


def _write(name: str, val) -> None:
    if not CG.exists():
        return
    try:
        (CG / name).write_text(str(val))
    except OSError as e:
        _log_once(name, f"cannot write {name}: {e}")


def main() -> int:
    RUN.mkdir(parents=True, exist_ok=True)
    lock = RUN / "gpu0.lock"
    os.close(os.open(lock, os.O_CREAT | os.O_RDWR, 0o666))
    os.chmod(lock, 0o666)
    _write("cgroup.freeze", 0)  # a crash/restart while frozen must not strand jobs
    ncpu = os.cpu_count() or 1
    link = _link_mbps()
    sm = {k: Smoother() for k in ("cpu", "ram", "gpu", "free")}
    prev = None  # (busy, total, swarm_usec)
    mem_state = (None, None)
    last_log = None
    while True:
        now = time.monotonic()
        try:
            busy, total = cpu_times(Path("/proc/stat").read_text())
            mem_total, mem_avail = _meminfo()
        except (OSError, ValueError) as e:
            print(f"governor: measure failed: {e}", flush=True)
            time.sleep(TICK_S)
            continue
        sw_usec, mem_cur, anon, present = measure_cg()
        if not present:
            _log_once("cg", f"{CG} missing; assuming no swarm usage, skipping cgroup writes")
        cpu_you = owner_cpu(prev, busy, total, sw_usec)
        prev = (busy, total, sw_usec)
        if cpu_you is None:  # first tick: owner use unknown, so no cap/ad yet (cpu_you=0 would mean 90%)
            time.sleep(TICK_S)
            continue
        ram_you = max(0.0, (mem_total - mem_avail - anon) / mem_total * 100)  # anon: cache is in MemAvailable

        gpu = read_gpu()
        your_vram, util_you = owner_gpu(gpu) if gpu else (0, 0)
        caps = {"cpu": sm["cpu"].push(now, cap_pct(cpu_you)),
                "ram": sm["ram"].push(now, cap_pct(ram_you)),
                "gpu": sm["gpu"].push(now, cap_pct(util_you))}

        if present:
            _write("cpu.max", f"{int(max(1000, caps['cpu'] * ncpu * 1000))} 100000")
            high = int(caps["ram"] / 100 * mem_total)
            _write("memory.high", high)
            mem_state, action = mem_step(mem_state, now, mem_cur, high)
            if action == "freeze":
                _write("cgroup.freeze", 1)
            elif action == "unfreeze":
                _write("cgroup.freeze", 0)
            elif action == "vacate":
                try:
                    subprocess.run(["condor_vacate"], capture_output=True, timeout=30)  # loses work since last save
                except (OSError, subprocess.SubprocessError) as e:
                    print(f"governor: condor_vacate failed: {e}", flush=True)
                finally:
                    _write("cgroup.freeze", 0)

        if gpu:
            raw_free = gpu_state(util_you, gpu["total_mb"], your_vram)["free_vram_mb"]
            free = int(sm["free"].push(now, raw_free))
            pub = gpu_state(90 - caps["gpu"], gpu["total_mb"], int(gpu["total_mb"] * 0.9) - free)
        else:
            pub = publish_gpu(None, 0, 0)
        try:
            _atomic(RUN / "gpu.json", json.dumps(pub))
            _atomic(RUN / "startd.ad", startd_ad(caps["cpu"], ncpu, link, 1 if gpu else 0, pub["free_vram_mb"],
                                                 caps["ram"], mem_total // 2**20, time.time()))
        except OSError as e:
            _log_once("publish", f"cannot publish: {e}")

        state = tuple(round(caps[k]) for k in ("cpu", "ram", "gpu")) + (pub["ok"], pub["free_vram_mb"])
        if state != last_log:
            print(f"governor: caps cpu={state[0]}% ram={state[1]}% gpu={state[2]}% "
                  f"gpu_ok={pub['ok']} free_vram_mb={pub['free_vram_mb']}", flush=True)
            last_log = state
        time.sleep(TICK_S)


if __name__ == "__main__":
    sys.exit(main())
