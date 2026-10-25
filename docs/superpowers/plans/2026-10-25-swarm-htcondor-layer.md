# swarm thin layer over HTCondor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a project describe a job once in `swarm.toml`, then run its shards across the always-on coordinator and any spare Linux worker. Workers are throttled live so the owner's own use always wins. Lost workers cost at most one hour of work.

**Architecture:** HTCondor 25.x (unmodified) handles the pool, matchmaking, file transfer and self-checkpointing. swarm adds four small pieces:
- `swarm submit` turns a `swarm.toml` into an HTCondor DAG: one cluster of shards, then a local-universe merge.
- A shard wrapper (`shard.py`) runs the project's `run` command. It turns "hourly save" into HTCondor's checkpoint exit code and handles GPU stop/start.
- A root governor on each worker caps HTCondor's job cgroup to `100% − owner use − 10%`. It also publishes machine attributes that worker `START` expressions use for the ≥20%-gain rule.
- A coordinator tick (systemd timer, every 60 s) removes dead workers from the collector and publishes the coordinator's free capacity into idle jobs.

**Tech Stack:** Python 3.12+ standard library only (`tomllib`, `unittest`), HTCondor 25.x from get.htcondor.org, systemd, cgroup v2, `nvidia-smi` when present.

**Spec:** `docs/superpowers/specs/2026-10-25-swarm-design.md` (read the Addendum) + `spike/RESULTS.md` (its gotchas are requirements).

## Global Constraints

- Python 3 standard library only; no pip dependencies. Tests use `python3 -m unittest`.
- HTCondor unmodified: configuration, submit files and CLI calls only. No fork, no patches.
- Zero AI tokens in any control loop (governor, tick, wrapper, submit).
- `swarm_cap = max(0, 100% − your_use − 10%)`, computed separately for CPU, RAM and GPU, every 2 s; shrink immediately, grow back after 30 s calm.
- Hourly save: `save_seconds` default `3600`. Worker stop is instant (< 2 s) and uploads nothing.
- Shards on workers run as the dedicated `swarm` user, never as the owner.
- Coordinator (claude, LXC 9901): never raise the Proxmox cpulimit (32); swarm jobs run at `CPUWeight=idle` + nice 19; do not disturb Harness fleets.
- Linux + systemd + cgroup v2 only.
- Spike gotchas are mandatory:
  - job `job_lease_duration = 180`
  - negotiator `NEGOTIATOR_PRE_JOB_RANK = (10000000 * My.Rank) + (1000000 * (RemoteOwner =?= UNDEFINED))`
  - schedd `SCHEDD_INTERVAL = 60`
  - worker `UPDATE_INTERVAL = 30`
  - the tick invalidates dead workers' ads after 3 missed polls
  - pin `NETWORK_INTERFACE`
  - use `TARGET.` in every machine reference inside job expressions

Deliberately deferred (v1), with reasons:
- **Tail duplicates:** a duplicate needs a copy of another job's spooled checkpoint; not worth it until a real job shows a slow tail.
- **Measured link rate:** `LINK_MBPS` is configured per worker (default 100). Measure it from `condor_history` transfer stats when move cost matters.
- **Expected output bytes in `move_cost`:** only input size is counted.

## Review Focus

1. Shard ids from `split` with spaces, `/`, shell characters, duplicates, or no output at all → `swarm submit` refuses with a message naming the bad id and builds nothing. (Task 3)
2. A `run` that ignores SIGTERM at the save point → the wrapper SIGKILLs its process group after 10 s and still exits 85. (Task 2)
3. A `run` that never writes to `{out}` → the wrapper still checkpoints `save_seconds + save_seconds/2` after start rather than running forever without a save. (Task 2)
4. A worker with no `nvidia-smi` (or one that errors) → the governor still caps CPU and RAM, and the GPU file says `ok: false`. (Task 4)
5. Re-submitting a job name that still has queued jobs → refused; otherwise the old state dir is replaced. (Task 3)

---

## File Structure

```
bin/swarm                 python shim → swarm.cli.main
swarm/__init__.py
swarm/jobspec.py          load + validate swarm.toml → JobSpec
swarm/shard.py            shard wrapper, runs on execute nodes; SELF-CONTAINED (stdlib only, no swarm imports)
swarm/submit.py           split, build DAG/submit files, condor_submit_dag
swarm/cli.py              submit | status | cancel | workers | join | leave
swarm/governor.py         worker governor (root service)
swarm/tick.py             coordinator tick (root, timer)
condor/coordinator.config template (placeholders @NAME@)
condor/worker.config      template
systemd/condor-instant-stop.conf   drop-in for condor.service on workers
systemd/condor-idle.conf           drop-in for condor.service on coordinator
systemd/swarm-governor.service
systemd/swarm-tick.service, systemd/swarm-tick.timer
install.sh                coordinator | worker <coord-ip> [--link-mbps N]
skill/SKILL.md            Claude Code skill "swarm"
tests/test_jobspec.py, test_shard.py, test_submit.py, test_cli.py, test_governor.py, test_tick.py
tests/e2e/toy/            swarm.toml + split/run/merge for acceptance
tests/e2e/e2e.sh
```

Runtime paths:
- **Coordinator:** state `~/.swarm/jobs/<name>/`; tick state `/var/lib/swarm/tick.json`; config `/etc/swarm/coordinator.conf` (`CPU_LIMIT=32`, `COORD_MACHINE=<fqdn>`).
- **Worker:** `/run/swarm/gpu.json`, `/run/swarm/startd.ad`, `/run/swarm/gpu0.lock`; config `/etc/swarm/worker.conf` (`LINK_MBPS=100`).
- **Code:** both roles install it to `/opt/swarm`, with `/usr/local/bin/swarm → /opt/swarm/bin/swarm`.

---

### Task 1: Job spec

**Files:**
- Create: `swarm/__init__.py` (empty), `swarm/jobspec.py`, `bin/swarm` (shim, used from Task 3)
- Test: `tests/test_jobspec.py`

**Interfaces:**
- Produces:
  - `class SpecError(ValueError)`
  - `NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")`
  - `SHARD_RE = re.compile(r"^[A-Za-z0-9._-]+$")`
  - `@dataclass(frozen=True) class JobSpec` with fields:
    - `name: str`, `jobdir: Path`, `workdir: Path` (absolute)
    - `gpu: str`, `vram_mb: int`, `threads: int`, `mem_mb: int`
    - `setup: str | None`, `check: str | None`
    - `split: str`, `run: str`, `merge: str`
    - `save_seconds: int`
  - `load(jobdir: Path) -> JobSpec`

- [ ] **Step 1: Write failing tests** (each writes a `swarm.toml` into a `tempfile.TemporaryDirectory`):
  - `test_minimal_defaults`: name/split/run/merge only → `workdir == jobdir`, `gpu == "no"`, `vram_mb == 0`, `threads == 1`, `mem_mb == 1024`, `save_seconds == 3600`, `setup is None`.
  - `test_unknown_key_rejected`: `thread = 4` → `SpecError` whose message contains `"thread"`.
  - `test_bad_gpu_value`: `gpu = "maybe"` → `SpecError`.
  - `test_gpu_needs_vram`: `gpu = "required"` without `vram_mb` → `SpecError`.
  - `test_run_needs_placeholders`: `run` missing `{out}` → `SpecError`; `merge` missing `{outs}` → `SpecError`.
  - `test_bad_name`: `name = "a b"` → `SpecError`.
  - `test_missing_workdir`: `workdir = "nope"` → `SpecError`.
- [ ] **Step 2:** `python3 -m unittest tests.test_jobspec -v` → FAIL (`ModuleNotFoundError`).
- [ ] **Step 3: Implement `load` in `swarm/jobspec.py`** using `tomllib`.
  - Required keys: `name`, `split`, `run`, `merge`.
  - Allowed keys: those plus `workdir`, `gpu`, `vram_mb`, `threads`, `mem_mb`, `setup`, `check`, `save_seconds`.
  - Integers must be `≥ 1` except `vram_mb ≥ 0`.
  - Resolve `workdir` relative to `jobdir`.
  - Write the `bin/swarm` shim: add the repo root to `sys.path` from `__file__`, then `sys.exit(swarm.cli.main(sys.argv[1:]))`.
- [ ] **Step 4:** rerun → PASS.
- [ ] **Step 5:** `git add swarm tests bin && git commit -m "feat: swarm.toml job spec"`

### Task 2: Shard wrapper

**Files:**
- Create: `swarm/shard.py`
- Test: `tests/test_shard.py`

**Interfaces:**
- Consumes (in the sandbox cwd, written by Task 3):
  - `workdir.tar.gz`
  - `job.json` = `{"run","check","setup","gpu","vram_mb","save_seconds","version"}`
- Consumes (written by Task 4): `$SWARM_RUN_DIR/gpu.json` = `{"ok": bool, "free_vram_mb": int}`; `SWARM_RUN_DIR` defaults to `/run/swarm`.
- Produces: `python3 shard.py <shard>`, `main(argv: list[str]) -> int`, with exit codes:

  | Code | Meaning |
  |---|---|
  | `0` | run finished |
  | `85` | checkpoint |
  | `86` | machine unfit |
  | any other | run's own exit code |

  - Sandbox layout: `./work/` (extracted workdir) and `./out/` (the shard's checkpointed state).
  - Constants: `KILL_GRACE = 10`, `POLL = float(os.environ.get("SWARM_POLL_SECONDS", 5))`.

Behaviour:
1. Extract `workdir.tar.gz` into `./work` unless `./work/.swarm-extracted` exists, then touch it.
2. Create `./out`.
3. If `check` is set and fails (run via `sh -c` in `./work`): run `setup` once per version. The marker is `/var/tmp/swarm-setup-<uid>/<version>`, created only when setup exits 0. Then re-check; a second failure → exit 86.
4. GPU mode:
   - `gpu = "no"` → CPU.
   - Otherwise GPU only if `gpu.json` has `ok` true and `free_vram_mb ≥ vram_mb`, and `gpu0.lock` can be taken with a non-blocking `flock`.
   - `required` without the GPU → wait, re-polling every `POLL`.
   - `optional` without the GPU → CPU.
5. Start `run` via `sh -c`, with `{shard}` / `{out}` substituted (`shlex.quote`) and cwd `./work`, in a new session.
   - Env gets `SWARM_GPU=1` + `CUDA_VISIBLE_DEVICES=0` in GPU mode, else `SWARM_GPU=0` + `CUDA_VISIBLE_DEVICES=""`.
6. Every `POLL` seconds:
   - If run exited, return its code.
   - If in GPU mode and `gpu.json` is no longer ok → kill the process group now. Release the lock and go back to step 4 (local `./out` is kept).
   - If `now − start ≥ save_seconds`, wait for the newest mtime under `./out` to be later than the moment the save point passed. Hard limit: `start + save_seconds × 1.5`. Then kill (SIGTERM to the group, SIGKILL after `KILL_GRACE`) and return 85.

- [ ] **Step 1: Write failing tests.** Each builds a sandbox in a temp dir: a tar of a `work` dir holding a tiny shell `run` script, plus `job.json`. Env: `SWARM_POLL_SECONDS=0.2` and `SWARM_RUN_DIR=<tmp>/run`.
  - `test_done_exit0`: `run` writes `{out}/r` and exits 0 → `main([...]) == 0`, `out/r` exists.
  - `test_run_failure_code`: `run` exits 3 → returns 3.
  - `test_checkpoint_after_write`: `save_seconds = 1`; `run` loops writing `{out}/p` every 0.3 s → returns 85 within 3 s; `out/p` exists.
  - `test_checkpoint_sigterm_ignored`: as above, but `run` does `trap '' TERM` → returns 85 within `1 + 1.5 + KILL_GRACE + 2` s.
  - `test_checkpoint_without_writes`: `save_seconds = 2`; `run` just sleeps → returns 85 after ≈3 s (not before 2.5 s).
  - `test_resume_skips_extract`: second `main` call in the same sandbox doesn't re-extract (a file written into `./work` survives).
  - `test_unfit`: `check = "false"`, `setup = "true"` → returns 86; `run` never starts (its marker file is absent).
  - `test_setup_once`: with `MARKER_ROOT` patched to a temp dir, `check = "test -f $HOME_OK"`, `setup = "touch $HOME_OK; echo x >> $COUNT"` (env vars to temp paths) → runs. Then delete `$HOME_OK`; a second `main` in a fresh sandbox with the same `version` skips setup (count file still has 1 line) and returns 86.
  - `test_gpu_optional_falls_back`: `gpu = "optional"`, `gpu.json` absent → run sees `SWARM_GPU=0` (it writes `$SWARM_GPU` to `{out}/mode`).
  - `test_gpu_revoked_restarts_on_cpu`: `gpu.json` `{"ok": true, "free_vram_mb": 4000}`, `vram_mb = 1000`, `optional`. `run` appends `$SWARM_GPU` to `{out}/mode` then sleeps 30. The test flips `gpu.json` to `ok: false` after 0.5 s → `mode` gets `1` then `0`.
- [ ] **Step 2:** `python3 -m unittest tests.test_shard -v` → FAIL.
- [ ] **Step 3: Implement `swarm/shard.py`**, with a `#!/usr/bin/env python3` shebang, mode 0755, and `if __name__ == "__main__": sys.exit(main(sys.argv[1:]))`. stdlib only (`fcntl`, `json`, `os`, `shlex`, `signal`, `subprocess`, `tarfile`, `time`). Module attribute `MARKER_ROOT = Path("/var/tmp")`. Extract with `tarfile.open(...).extractall(work, filter="data")`.
- [ ] **Step 4:** rerun → PASS.
- [ ] **Step 5:** `git commit -am "feat: shard wrapper (hourly checkpoint, GPU stop/start, setup/check)"`

### Task 3: Submit, status, cancel, workers

**Files:**
- Create: `swarm/submit.py`, `swarm/cli.py`
- Test: `tests/test_submit.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `jobspec.load`, `JobSpec`, `SHARD_RE`, `SpecError` (Task 1); `swarm/shard.py` path (Task 2).
- Produces:
  - `split(spec: JobSpec) -> list[str]` (raises `SpecError`)
  - `build(spec: JobSpec, shards: list[str], statedir: Path) -> Path` (returns the `.dag` path)
  - `submit(jobdir: Path, state_root: Path = Path.home() / ".swarm/jobs") -> str`
  - `cli.main(argv: list[str]) -> int`
  - `cli.summarize(name: str, total: int, done: int, rows: list[tuple[str, int, str]]) -> str`; rows are `(JobBatchName, JobStatus, RemoteHost)`
- Job ads carry custom attrs: `SwarmJob = "<name>"`, `SwarmInputMB = <float>`, `SwarmGpu = "<no|optional|required>"`. Task 5 qedits `SwarmCoordRate`.

`build` writes into `statedir` (wipes it first):
- `workdir.tar.gz`: `tar -czf … -C workdir .`, with `--exclude-from=workdir/.swarmignore` when that file exists.
- `job.json` (fields per Task 2; `version` = sha256 of the tarball, first 16 hex chars)
- `shards.txt`, `shards/<id>/` for each shard
- `merge.sh`: `cd <jobdir> && exec <merge>`, where `{outs}` = space-joined `shlex.quote` of `statedir/shards/<id>/out` in shard order
- `job.dag`:
  ```
  JOB shards shards.sub
  JOB merge merge.sub
  PARENT shards CHILD merge
  ```
- `merge.sub`: `universe = local`, `executable = merge.sh`, plus `output`/`error`/`log` = `merge.out`/`merge.err`/`merge.log`.
- `shards.sub` must contain exactly these lines, in this order:

```
executable                = /opt/swarm/swarm/shard.py
arguments                 = $(shard)
initialdir                = shards/$(shard)
transfer_input_files      = <statedir>/workdir.tar.gz, <statedir>/job.json
should_transfer_files     = yes
checkpoint_exit_code      = 85
transfer_checkpoint_files = out
transfer_output_files     = out
job_lease_duration        = 180
max_retries               = 3
job_machine_attrs         = Machine
requirements              = (MY.ExitCode =!= 86 || TARGET.Machine =!= MY.MachineAttrMachine0)<GPU_REQ>
rank                      = <GPU_RANK>
request_cpus              = <threads>
request_memory            = <mem_mb>
request_disk              = <2 × tarball MB + 1024>M
+SwarmJob                 = "<name>"
+SwarmInputMB             = <tarball MB, 3 decimals>
+SwarmGpu                 = "<gpu>"
output = run.out
error  = run.err
log    = <statedir>/shards.log
queue shard from shards.txt
```

GPU placeholders by `gpu` value:

| `gpu` | `<GPU_REQ>` | `<GPU_RANK>` |
|---|---|---|
| `required` | ` && TARGET.SwarmGpus >= 1 && TARGET.SwarmGpuFreeMB >= <vram_mb>` | `0` |
| `optional` | empty | `ifThenElse(TARGET.SwarmGpuFreeMB >= <vram_mb>, 1, 0)` |
| `no` | empty | `0` |

`executable` is the installed path (`/opt/swarm/...`); tests take it from `SWARM_SHARD_PY` env, defaulting to that path.

`submit`:
1. `load`, then `split` (run `spec.split` via `sh -c` in `jobdir`). Every line stripped, blank lines dropped. Each id must match `SHARD_RE` and be unique, and there must be ≥1 id; otherwise `SpecError` naming the offending line.
2. Refuse if `condor_q -af JobBatchName` lists the name.
3. `build`, then run `condor_submit_dag -batch-name <name> job.dag` in `statedir`.

CLI:
- `submit <jobdir>` → prints the name.
- `status [name]` → `summarize` per job state dir under `~/.swarm/jobs`. `done` = count of `shards/<id>/out` dirs; `rows` from `condor_q -af JobBatchName JobStatus RemoteHost`. Merge state comes from whether `merge.log` contains `Job terminated`.
- `cancel <name>` → `condor_rm -constraint 'JobBatchName == "<name>"'`.
- `workers` → prints `condor_status -af Machine State Cpus SwarmCapCores SwarmGpuFreeMB`.
- `join <coord-ip> [--link-mbps N]` → `os.execvp("sudo", ["sudo", "/opt/swarm/install.sh", "worker", ...])`; when `/opt/swarm` is absent, use the repo's own `install.sh`.
- `leave` → `sudo systemctl disable --now swarm-governor condor`.
- `SpecError` → message on stderr, exit 2.

- [ ] **Step 1: Write failing tests:**
  - `test_build_files` (toy spec, 3 shards, temp statedir; `SWARM_SHARD_PY=/x/shard.py`): every line above is present in `shards.sub` with values filled in; `job.dag` text equals the 3 lines; `merge.sh` contains the 3 quoted out paths in order; the 3 `shards/<id>/` dirs exist.
  - `test_gpu_required_lines`: `requirements` ends with `&& TARGET.SwarmGpus >= 1 && TARGET.SwarmGpuFreeMB >= 2000`.
  - `test_swarmignore`: a file listed in `.swarmignore` is absent from `workdir.tar.gz`.
  - `test_split_rejects` (subTests): `split` printing `a b`, `../x`, `a\na` and empty output each → `SpecError`; the message contains the bad id (except for empty output).
  - `test_resubmit_refused`: patch `subprocess.run` so `condor_q` returns the name → `submit` raises `SpecError` and `build` is never called.
  - `test_summarize` (`tests/test_cli.py`): `summarize("j", 6, 2, [("j",2,"slot1_1@NathanielSong"),("j",1,"undefined")])` contains `j`, `2/6 done`, `1 running`, `NathanielSong`, `1 idle`.
- [ ] **Step 2:** `python3 -m unittest tests.test_submit tests.test_cli -v` → FAIL.
- [ ] **Step 3: Implement `swarm/submit.py`, `swarm/cli.py`** (`argparse`).
- [ ] **Step 4:** rerun → PASS; also `python3 -m unittest -v` (all tests).
- [ ] **Step 5:** `git commit -am "feat: swarm submit/status/cancel/workers"`

### Task 4: Worker governor

**Files:**
- Create: `swarm/governor.py`
- Test: `tests/test_governor.py`

**Interfaces:**
- Produces:
  - `cap_pct(your_pct: float) -> float`
  - `class Smoother(window_s: float = 30)` with `.push(t: float, raw: float) -> float`: the min of raw values pushed within the last `window_s`.
  - `cpu_times(stat_text: str) -> tuple[int, int]`: `(busy, total)` jiffies from the `/proc/stat` "cpu" line; busy = total − idle − iowait.
  - `parse_nvidia(gpu_csv: str, apps_csv: str) -> dict`, returning `{util_pct, total_mb, used_mb, procs: {pid: mb}}`.
    - `gpu_csv` comes from `nvidia-smi --query-gpu=utilization.gpu,memory.total,memory.used --format=csv,noheader,nounits` (first GPU only).
    - `apps_csv` comes from `--query-compute-apps=pid,used_memory --format=csv,noheader,nounits`.
  - `gpu_state(util_pct: float, total_mb: int, your_vram_mb: int) -> dict`, returning `{"ok": bool, "free_vram_mb": int}`:
    - `ok = cap_pct(util_pct) > 0 and free_vram_mb > 0`
    - `free_vram_mb = int(total_mb * 0.9 − your_vram_mb)`, floored at 0
  - `startd_ad(cap_cpu_pct: float, ncpu: int, link_mbps: float, gpus: int, gpu_free_mb: int) -> str`, emitting lines:
    - `SwarmCapCores = <floor(cap_cpu_pct * ncpu / 100)>`
    - `SwarmLinkMbps = <link_mbps>`
    - `SwarmGpus = <gpus>`
    - `SwarmGpuFreeMB = <gpu_free_mb>`
  - `main() -> int`: the loop. Every module with a `main` ends with `if __name__ == "__main__": sys.exit(main())`.
- Constant `CG = Path("/sys/fs/cgroup/system.slice/condor.service/htcondor")` (verified in the spike).

Loop, every 2 s:
1. Measure:
   - CPU from `/proc/stat` and `CG/cpu.stat usage_usec` deltas.
   - RAM: `MemTotal − MemAvailable` vs `CG/memory.current`.
   - GPU: `nvidia-smi`. Swarm's GPU use = processes whose `/proc/<pid>/cgroup` path contains `/htcondor/`. GPU util is not attributable per process, so `your_util = total util` when swarm has no GPU process, else `0`. This is the known ceiling: a swarm GPU job hides the owner's utilisation, but VRAM is still attributed exactly.
2. Compute `your_pct` for each resource, then `cap_pct`, then one `Smoother` per resource.
3. Apply:
   - Write `CG/cpu.max` = `"<max(1000, cap × ncpu × 1000)> 100000"`.
   - Write `CG/memory.high` = `int(cap_ram% × MemTotal)` bytes.
   - If `memory.current > memory.high` for > 30 s, write `1` to `CG/cgroup.freeze`. If still over 30 s later, run `condor_vacate` (loses work since last save; ceiling noted) and unfreeze. Unfreeze as soon as it is back under.
4. Atomically write `/run/swarm/gpu.json` (from `gpu_state` on smoothed values) and `/run/swarm/startd.ad`. Create `/run/swarm/gpu0.lock` mode 0666 when missing. If `nvidia-smi` is missing or errors: `{"ok": false, "free_vram_mb": 0}`, `SwarmGpus = 0`.

- [ ] **Step 1: Write failing tests:**
  - `test_cap_pct`: `cap_pct(20) == 70`, `cap_pct(30) == 60`, `cap_pct(0) == 90`, `cap_pct(95) == 0`.
  - `test_smoother`: push `(0, 70)`, `(1, 20)` → 20; push `(20, 70)` → 20; push `(31.5, 70)` → 70.
  - `test_cpu_times`: from a two-line `/proc/stat` sample, returns the expected `(busy, total)`.
  - `test_parse_nvidia`: `"14, 8192, 1736"` + `"1234, 500\n5678, 300"` → `util_pct == 14`, `total_mb == 8192`, `used_mb == 1736`, `procs == {1234: 500, 5678: 300}`.
  - `test_gpu_state`: `(50, 8192, 1000)` → `ok True`, `free 6372`; `(95, 8192, 0)` → `ok False`; `(0, 8192, 8000)` → `ok False`, `free 0`.
  - `test_startd_ad`: `startd_ad(60, 20, 100, 1, 6372)` contains `SwarmCapCores = 12` and `SwarmGpuFreeMB = 6372`.
  - `test_no_nvidia` (Review Focus 4): with `shutil.which` patched to return None, `read_gpu()` returns None and the published GPU dict is `{"ok": False, "free_vram_mb": 0}`.
- [ ] **Step 2:** `python3 -m unittest tests.test_governor -v` → FAIL.
- [ ] **Step 3: Implement `swarm/governor.py`**, plus `read_gpu() -> dict | None`, which wraps `nvidia-smi` and returns None on missing binary or non-zero exit.
- [ ] **Step 4:** rerun → PASS.
- [ ] **Step 5:** `git commit -am "feat: worker governor (cap = 100 - use - 10)"`

### Task 5: Coordinator tick

**Files:**
- Create: `swarm/tick.py`
- Test: `tests/test_tick.py`

**Interfaces:**
- Consumes: job attr `SwarmJob` (Task 3); `/etc/swarm/coordinator.conf` with `CPU_LIMIT` and `COORD_MACHINE` (Task 6).
- Produces:
  - `parse_addr(my_address: str) -> tuple[str, int]`
  - `update_misses(misses: dict[str, int], alive: dict[str, bool]) -> tuple[dict[str, int], list[str]]`: returns the new counts and the machines that just reached 3 misses (whose counts are reset to 0).
  - `coord_free_cores(cpu_limit: float, total_usec: int, jobs_usec: int, dt_s: float) -> float`: `max(0, cpu_limit − (total_usec − jobs_usec) / 1e6 / dt_s)`, where the args are deltas.
  - `main() -> int`
  - Job attr `SwarmCoordRate` (float cores), qedited on idle swarm jobs.

`main`, one run per timer tick:
1. Load `/var/lib/swarm/tick.json` (misses, last counters, last time).
2. Get workers from `condor_status -af Machine MyAddress`, skipping `COORD_MACHINE`. Mark one alive if a TCP connect to `parse_addr(...)` succeeds within 3 s.
3. For each machine returned by `update_misses`:
   - Write a temp ad: `MyType = "Query"`, `TargetType = "Machine"`, `Requirements = TARGET.Machine == "<m>"`.
   - Run `condor_advertise INVALIDATE_STARTD_ADS <file>`.
4. Coordinator free cores:
   - total = `/sys/fs/cgroup/cpu.stat usage_usec` (container root)
   - jobs = `CG/cpu.stat usage_usec` (same `CG` path as Task 4)
   - `coord_free_cores` uses the deltas since the last tick; skip on the first run.
5. `condor_qedit -constraint 'SwarmJob =!= undefined && JobStatus == 1' SwarmCoordRate <v:.1f>`
6. `condor_reschedule` if anything was invalidated.
7. Save state.

- [ ] **Step 1: Write failing tests:**
  - `test_parse_addr`: `"<192.168.1.123:9618?addrs=192.168.1.123-9618+[fd7a-115c-a1e0--6b34-b26e]-9618&alias=NathanielSong&noUDP&sock=startd_1_2>"` → `("192.168.1.123", 9618)`.
  - `test_update_misses`: three calls with `{"A": False, "B": True}` → `[]`, `[]`, `["A"]`, and the counts end at `{"A": 0, "B": 0}`. A call with A dead, then A alive → A's count is 0.
  - `test_coord_free_cores`: `(32, 20e6*60, 4e6*60, 60)` → `16.0`; `(32, 40e6*60, 0, 60)` → `0`.
- [ ] **Step 2:** `python3 -m unittest tests.test_tick -v` → FAIL.
- [ ] **Step 3: Implement `swarm/tick.py`.**
- [ ] **Step 4:** rerun → PASS.
- [ ] **Step 5:** `git commit -am "feat: coordinator tick (dead-worker invalidation, coord rate)"`

### Task 6: Configs, units, install, deploy

**Files:**
- Create: `condor/coordinator.config`, `condor/worker.config`, `systemd/*` (5 files per File Structure), `install.sh`
- Test: `tests/test_gate.py`

**Interfaces:**
- Consumes: Tasks 1–5 (installed to `/opt/swarm`).
- Produces the attrs and expressions that Tasks 2–5 rely on, on the live pool.

`condor/coordinator.config`:

```
use role:get_htcondor_submit
use role:get_htcondor_execute
NETWORK_INTERFACE = @IP@
NUM_CPUS = @CPU_LIMIT@
MEMORY = @MEMORY_MB@
JOB_RENICE_INCREMENT = 19
CGROUP_MEMORY_LIMIT_POLICY = none
SCHEDD_INTERVAL = 60
NEGOTIATOR_PRE_JOB_RANK = (10000000 * My.Rank) + (1000000 * (RemoteOwner =?= UNDEFINED))
```

`condor/worker.config`:

```
NETWORK_INTERFACE = @IP@
UPDATE_INTERVAL = 30
CGROUP_MEMORY_LIMIT_POLICY = none
SUSPEND = False
PREEMPT = False
STARTD_CRON_JOBLIST = SWARM
STARTD_CRON_SWARM_EXECUTABLE = /bin/cat
STARTD_CRON_SWARM_ARGS = /run/swarm/startd.ad
STARTD_CRON_SWARM_MODE = Periodic
STARTD_CRON_SWARM_PERIOD = 10s
SWARM_GATE = isUndefined(TARGET.SwarmCoordRate) || (MY.SwarmCapCores * (60 - TARGET.SwarmInputMB * 8 / MY.SwarmLinkMbps / 60 - 1) >= 0.2 * TARGET.SwarmCoordRate * 60)
START = (MY.SwarmCapCores =!= undefined) && (MY.TotalSlotCpus - MY.Cpus + TARGET.RequestCpus <= MY.SwarmCapCores) && $(SWARM_GATE)
DEDICATED_EXECUTE_ACCOUNT_REGEXP = swarm
STARTER_ALLOW_RUNAS_OWNER = False
@SLOT_USERS@
```

`@SLOT_USERS@` = lines `SLOT1_<i>_USER = swarm` for i in 1..nproc. Step 4 checks this and falls back if it doesn't work.

systemd units:
- `systemd/condor-instant-stop.conf` (→ `/etc/systemd/system/condor.service.d/` on workers): `[Service]` `KillMode=control-group`, `KillSignal=SIGKILL`, `TimeoutStopSec=1s`.
- `systemd/condor-idle.conf` (coordinator): `[Service]` `CPUWeight=idle`.
- `swarm-governor.service`: `WorkingDirectory=/opt/swarm`, `ExecStart=/usr/bin/python3 -m swarm.governor`, `Restart=always`, `RuntimeDirectory=swarm`, `RuntimeDirectoryPreserve=yes`, `Before=condor.service`.
- `swarm-tick.service` (oneshot, `WorkingDirectory=/opt/swarm`, `ExecStart=/usr/bin/python3 -m swarm.tick`; tick imports `CG` from `swarm.governor`) + `swarm-tick.timer` (`OnBootSec=60`, `OnUnitActiveSec=60`).

`install.sh` (bash, `set -euo pipefail`, must run as root; idempotent):
- **Common:**
  - `rsync -a --delete <repo>/ /opt/swarm/` (excluding `.git`)
  - `ln -sf /opt/swarm/bin/swarm /usr/local/bin/swarm`
  - Fill templates with `sed`; `@IP@` = `ip -4 route get <coord-ip or 1.1.1.1> | awk` src.
- **`coordinator`:**
  - If `condor_version` is missing, run get.htcondor.org `--cm <IP>` (password from `GET_HTCONDOR_PASSWORD` or a prompt).
  - Write `/etc/condor/config.d/99-swarm.config`; `rm -f 99-swarm-spike.config`.
  - `MEMORY_MB` = MemTotal/1024 from `/proc/meminfo`; `CPU_LIMIT` from `--cpu-limit N`, default `nproc`. claude gets `--cpu-limit 32`.
  - Write `/etc/swarm/coordinator.conf` (`CPU_LIMIT`, `COORD_MACHINE=$(condor_config_val FULL_HOSTNAME)`).
  - Install the idle drop-in, enable `swarm-tick.timer`, `systemctl daemon-reload`, `systemctl restart condor`.
  - Copy `skill/` to `~<SUDO_USER or --user>/.claude/skills/swarm/`.
- **`worker <coord-ip> [--link-mbps N]`:**
  - If missing, run get.htcondor.org `--ep <coord-ip>`.
  - `id swarm || useradd --system --create-home --shell /usr/sbin/nologin swarm`
  - Write `/etc/swarm/worker.conf` (`LINK_MBPS`, default 100) and `99-swarm.config` (`rm -f 99-swarm-spike.config`).
  - Install the instant-stop drop-in, enable and start `swarm-governor`, `systemctl daemon-reload`, `systemctl restart condor`.

- [ ] **Step 1: Write the placement-gate test `tests/test_gate.py`.** This is spec acceptance 1 for the placement formula.
  - Read the `SWARM_GATE = …` line from `condor/worker.config` and evaluate it with HTCondor's `classad_eval` (check the exact flags with `classad_eval -help`). Skip with `unittest.skipUnless(shutil.which("classad_eval"))`.
  - `test_gate_adds_20pct`: my `SwarmCapCores=12; SwarmLinkMbps=100`, target `SwarmInputMB=50; SwarmCoordRate=16` → true.
  - `test_gate_too_small`: `SwarmCapCores=2`, same target → false (2 × 58.9 < 192).
  - `test_gate_no_coord_rate`: target without `SwarmCoordRate` → true.
  - Run `python3 -m unittest tests.test_gate -v` on the desktop (condor installed) → FAIL until the config exists.
- [ ] **Step 1b: Write the files above.** Then `bash -n install.sh` → no output, and `python3 -m unittest tests.test_gate -v` → PASS.
- [ ] **Step 2: Deploy the coordinator:**
  ```
  ssh claude 'git clone git@github.com:VocanicZ/swarm.git ~/swarm || git -C ~/swarm pull'
  ssh root@192.168.1.100 'pct exec 9901 -- env SUDO_USER=claude bash /home/claude/swarm/install.sh coordinator --cpu-limit 32'
  ```
  Verify with `ssh claude 'condor_config_val NUM_CPUS MEMORY SCHEDD_INTERVAL; systemctl show condor -p CPUWeight; systemctl list-timers swarm-tick.timer'`.
  Expected: `32`, `65536` (±), `60`, `CPUWeight=idle`, timer listed.
- [ ] **Step 3: Deploy the worker (owner runs it):**
  ```
  ! sudo bash ~/swarm/install.sh worker 192.168.1.111
  ```
  Verify with `ssh claude 'condor_status -af Machine SwarmCapCores SwarmGpus SwarmGpuFreeMB'`: the `NathanielSong` row has numbers, not `undefined`, within 60 s.
- [ ] **Step 4: Check the shard user.** Submit a 1-shard job whose `run` is `id -un > {out}/who` (requirement `TARGET.Machine == "NathanielSong"`, via a temporary edit of the generated `shards.sub`). `who` must contain `swarm`.
  - If it says `nobody`, try `SLOT_TYPE_1_USER`-style knobs per the current HTCondor docs (`condor_config_val -dump | grep -i user`).
  - If nothing works, stop and report to the owner: `nobody` cannot read their 750 home, which may be acceptable.
- [ ] **Step 5: Check instant stop.** Owner runs `! time sudo systemctl stop condor && sudo systemctl start condor` → `real` < 2 s.
- [ ] **Step 6:** `git add -A && git commit -m "feat: install.sh, condor configs, systemd units" && git push`

### Task 7: Skill

**Files:**
- Create: `skill/SKILL.md`

- [ ] **Step 1: Write `skill/SKILL.md`.**
  - Frontmatter: `name: swarm`, plus a description: "Run CPU/GPU-heavy batch work across spare machines via `swarm submit`. Use when a job splits into independent resumable shards…".
  - Body:
    - When to use: long, shardable, resumable work. Not agent sessions.
    - The `swarm.toml` contract from spec §Job contract, verbatim key list plus `mem_mb` / `save_seconds`.
    - The `run` rules: resume from `{out}`, write progress at least every 30 min, write atomically, honour `SWARM_GPU`.
    - The CLI commands and how to read `swarm status`.
    - That `split` ids must match `[A-Za-z0-9._-]+`.
  - Keep it under 100 lines.
- [ ] **Step 2:** re-run `install.sh coordinator` (Task 6 Step 2 command) → `ssh claude 'ls ~/.claude/skills/swarm/SKILL.md'` exists.
- [ ] **Step 3:** `git commit -am "feat: swarm Claude Code skill" && git push`

### Task 8: Acceptance on the real pool

**Files:**
- Create: `tests/e2e/toy/swarm.toml`, `tests/e2e/toy/split.sh`, `tests/e2e/toy/run.sh`, `tests/e2e/toy/merge.sh`, `tests/e2e/expected.txt`, `tests/e2e/e2e.sh`

Toy job:
- `swarm.toml`: `name = "toy"`, `threads = 1`, `save_seconds = 60`.
- `split.sh` prints `s1`..`s6`.
- `run.sh {shard} {out}`: resumes the counter from `{out}/n` (default 0) and counts to 240 at one step per second. It rewrites `{out}/n` atomically every 10 s. At the end it writes `{out}/result` = `<shard> <sum of 1..240>`.
- `merge.sh {outs}`: `cat` each `result`, sort, write `merged.txt` into the toy dir.
- `expected.txt` = the 6 sorted lines.
- `e2e.sh` (runs on claude):
  1. `swarm submit tests/e2e/toy`.
  2. Poll `swarm status toy` until a shard is running on `NathanielSong` and ≥1 checkpoint exists under `$(condor_config_val SPOOL)`. Then print `KILL WORKER NOW` and wait for the owner's step.
  3. Poll until `merged.txt` exists (timeout 30 min).
  4. `diff merged.txt expected.txt` → exit 0.

- [ ] **Step 1: Write the files.** Run the toy locally once without condor (loop `split`/`run`/`merge` in a temp dir) → `diff` passes.
- [ ] **Step 2:** `ssh claude 'cd ~/swarm && git pull && tests/e2e/e2e.sh'`. At `KILL WORKER NOW`, the owner runs `! time sudo systemctl stop condor`.
  - Expected: stop `real` < 2 s.
  - Within ~4 min (3 missed ticks + lease 180 s), the killed shards run on claude. `shards.log` / `run.out` shows their counter starting from the checkpoint, not 0.
  - The script exits 0. This is acceptance 2.
- [ ] **Step 3: Acceptance 3.**
  1. Owner restarts condor, then runs a CPU stress (e.g. `stress-ng --cpu 10` or `python3 -c` busy loops ×10).
  2. While watching `journalctl -u swarm-governor -f` (have the governor log the caps once per change) and `condor_status -af Machine SwarmCapCores`: within ~5 s, `cpu.max` drops to ≈ (100 − your − 10)% of 20 cores.
  3. Stop the stress; the caps recover ~30 s later.
  4. Then start a GPU load (a game, or any CUDA demo) and confirm `/run/swarm/gpu.json` turns `ok: false` within ~5 s.
- [ ] **Step 4:** Write the results into `HANDOFF.md` (state = v1 built, acceptance results), then `git commit -am "test: e2e acceptance toy job" && git push`.
