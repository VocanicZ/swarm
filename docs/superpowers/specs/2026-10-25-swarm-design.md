# swarm: spare-machine workers for an always-on coordinator

Date: 2026-10-25 · Owner: VocanicZ · Status: draft for approval

## Why

The owner runs long jobs on an always-on but CPU-capped host (the "claude machine": Proxmox LXC,
64 threads capped at 50% total, no GPU). Their desktop PC (20 threads, 125 GB RAM, RTX 3060 Ti 8 GB)
sits mostly idle but is switched off every night and used for games. They want the desktop, and any
future machine, to join as a **dynamic worker**: when it is on, it takes a share of the work and
makes the job finish sooner; when it goes away, nothing is lost beyond the last save and the
always-on host carries on alone. It must never get in the way of the owner using the PC.

Built once, reused on any project and installable on any Linux machine (repo `VocanicZ/swarm`).

## Goals

1. A job finishes sooner when a worker is up than on the coordinator alone, or at least no later.
2. **The owner's own use of a worker always wins.** The worker's own workload ("your stuff") is
   primary; swarm work is capped dynamically to what is left minus 10% headroom, for CPU, RAM and GPU
   each: `swarm_cap = 100% − your_use − 10%` (floored at 0). 20% your use → swarm 70%; 30% → 60%.
   Re-evaluated every ~2 s, no user action ever needed (start a game → swarm shrinks within seconds).
3. **Assume every worker is only up for 1 hour.** Work sent to a worker is sized for one hour;
   progress is saved back to the coordinator every hour. Shutting a worker down is always instant:
   nothing is uploaded at shutdown, work since the last save is discarded, and the coordinator
   re-places that work (from the last save) on another worker or runs it itself.
4. **Zero AI tokens in the control loop.** Placement, capping, checkpointing are plain formulas and
   code. No LLM calls, ever, in the daemon paths.
5. Any number of workers (a cluster of dynamic workers), joining and leaving at any time.
6. Reusable: a CLI + a Claude Code skill so any project (or agent) can submit work.

## Non-goals (v1)

- Migrating an arbitrary running process. Jobs describe themselves once (see Job contract).
- Running Claude/agent sessions on workers. Agents are API-bound, not CPU-bound; Harness fleets keep
  running on the coordinator. (A Harness job type can come later.)
- An MCP server (the CLI + skill cover it; add MCP when a non-Claude-Code client needs it).
- Windows/macOS workers. Linux with systemd + cgroup v2 only.
- Scheduling between multiple coordinators, auth beyond SSH, a web UI.

## Anti-goals (failure even if it "works")

- A game stutters or the desktop feels slow because of swarm.
- More than ~1 h of work lost when a worker disappears, however it goes away.
- Swarm delaying the owner's shutdown or reboot by even a few seconds (stop = kill, no upload).
- Spending AI usage to make scheduling decisions.
- The coordinator exceeding its own host rules (the claude machine's 50%-per-core cap is enforced
  by Proxmox; swarm must not try to work around it).

## Shape

```
coordinator (always on, e.g. claude machine)          worker (e.g. desktop PC), N of them
  swarm-coord daemon                                  swarm-worker daemon (systemd --user)
   ├ job queue + shard state  (~/.swarm/)              ├ governor: every 2 s set caps on swarm.slice
   ├ runs shards locally (its own capacity)            ├ runs shards it was handed, inside swarm.slice
   ├ hourly: placement formula per worker             ├ hourly + on stop: push checkpoint to coord
   └ pulls checkpoints, re-queues lost shards         └ heartbeat (coordinator polls over SSH)
```

Transport is **SSH + rsync** only (the coordinator already reaches workers by SSH/Tailscale). No
new network service, no open ports. The coordinator drives: it pushes shard inputs, starts shards,
pulls checkpoints. Workers are listed in `~/.swarm/workers` on the coordinator; `swarm join
<coordinator>` run on a new machine installs the worker service and appends itself there (dynamic).

## Job contract (what a project writes once)

A job is a directory with a `swarm.toml`:

```toml
name    = "hg2-power-fit-v4i"
workdir = "."                      # synced to workers (rsync, excludes from .swarmignore)
gpu     = "optional"               # "no" | "optional" | "required"
vram_mb = 2000                     # GPU memory a GPU shard needs (gpu != "no")
threads = 4                        # per shard
setup   = "tools/setup.sh"         # optional: idempotent, run once per worker per job version
check   = "tools/check.sh"         # optional: exits 0 if this machine can run shards (deps present)
split   = "tools/split.sh 200"     # prints one shard id per line (run once, on coordinator)
run     = "tools/run_shard.sh {shard} {out}"   # must RESUME from {out} if it has partial state
merge   = "tools/merge.sh {outs}"  # run on coordinator when every shard is done
```

Rules: a shard's only state is its `{out}` dir; `run` must be safe to restart and continue from
whatever is in `{out}`. `run` writes progress into `{out}` **at least every 30 min**, and writes each
file **atomically** (write temp file, then rename), so a copy taken at any moment is consistent.
`run` sees `SWARM_GPU=1` (and `CUDA_VISIBLE_DEVICES`) when it may use the GPU, `SWARM_GPU=0` when it
must use CPU (`gpu=optional` shards switch between the two across restarts). Saving = rsync `{out}`
back. That is the whole checkpoint mechanism; no process snapshots.

Shards should be sized to finish in well under an hour on one worker (guideline: 5–30 min), so
"sized for one hour" is achieved by how many shards a worker gets, not by shard length.

Dependencies: a worker runs `check` before taking shards of a job; if it fails it runs `setup`
(once per job version) and re-checks; still failing → that worker is marked unfit for the job.

## Placement formula (coordinator; runs on events, not just hourly)

Placement runs when a worker joins / comes back, when a shard finishes, when a governor reports
freed capacity, and at the hourly tick (which is also the save). All plain code, zero tokens.

```
pc_free    = worker's swarm cap right now (cores, RAM, GPU), as reported by its governor
move_cost  = minutes to send inputs + bring results back
           = (workdir_delta_bytes + expected_out_bytes) / measured_link_rate + start_overhead
pc_minutes = 60 − move_cost
coord_rate = coordinator cores swarm may use = free CPU measured over the last minute, within the
             host's own cap (the coordinator runs swarm shards at idle priority, see below)

gpu=required          → worker with a GPU only (else waits)
gpu=optional          → prefer a GPU worker
shards available      → give worker k shards if  pc_free_cores × pc_minutes ≥ 0.2 × coord_rate × 60
                        (worker adds ≥ 20% this hour); k = floor(pc_free_cores / threads)
otherwise             → nothing sent this hour
```

`measured_link_rate` is measured by the rsync itself and remembered per worker (the owner's PC is on
a 100 Mb/s link today). Shards stay assigned to a worker until done or the worker is unreachable (coordinator polls workers
every minute; 3 missed polls = gone); then they are re-queued from their last pulled `{out}`.

Tail of a job (best-effort "never later than coordinator alone"): when the queue is empty and the
coordinator has idle capacity, it starts a duplicate of each shard still running on a worker from
that shard's last pulled `{out}`; whichever finishes first wins, the other is stopped.

Coordinator side: local shards run in its own `swarm.slice` with `CPUWeight=idle` and `Nice=19`,
so anything else on the coordinator (Harness fleets, agents' builds) always wins. The claude
machine's Proxmox cpulimit stays the hard ceiling. (cpu+memory controllers are delegated to the user
systemd there; verified.)

## Governor (worker, every ~2 s)

- Measure total CPU, RAM and GPU use; subtract swarm's own use (cgroup stats of `swarm.slice`, GPU
  per-process via `nvidia-smi` mapped to pids in the slice) → `your_use` per resource.
- `cap = max(0, 100% − your_use − 10%)` per resource.
- CPU: `systemctl --user set-property --runtime swarm.slice CPUQuota=…`.
- RAM: `MemoryHigh` to the cap; if swarm is over the cap for >30 s, freeze the slice; if your_use
  keeps rising so swarm cannot fit, checkpoint-push and stop the shard (re-queued).
- GPU: same rule on GPU utilisation and on VRAM: a GPU shard may run only while
  `100% − your_gpu_util − 10%` is above zero AND free VRAM after the 10% headroom ≥ its `vram_mb`.
  There is no hard GPU cap, so enforcement is stop/start: when the rule fails (e.g. a game starts),
  GPU shards are stopped (frees VRAM immediately) and restart from `{out}` later, or on CPU when
  `gpu=optional`. Since `run` writes progress every ≤30 min, a stop loses at most that.
- Smoothing so a brief spike does not stop work: shrink immediately, grow back after ~30 s calm.

## Saving

- Hourly (coordinator-driven, at the placement tick): pull every running shard's `{out}`.
- Shutdown/reboot (graceful or not): **no upload**. The worker unit stops instantly (`KillMode=mixed`,
  short `TimeoutStopSec`); whatever was not yet pulled is discarded. Owner's decision.
- The coordinator notices the worker is gone (3 missed one-minute polls) and re-queues its shards
  from their last pulled `{out}`, to be placed on another worker or run on the coordinator.
- When the worker comes back, any stale local `{out}` copies are discarded; the coordinator's copy
  is the only truth.

## Security

Shards run on a worker as a **dedicated `swarm` user** (linger enabled, member of the GPU device
groups, no access to the owner's home), never as the owner. The coordinator reaches it with its own
SSH key, restricted to that user. Reason: autonomous agents on the coordinator may `swarm submit`,
and must not be able to run code as the owner on the desktop. Installing a worker needs sudo once.

## Interfaces

- CLI: `swarm submit <jobdir>`, `swarm status`, `swarm cancel <job>`, `swarm join <coord>`,
  `swarm leave`, `swarm workers`.
- Claude Code skill `swarm`: when to use it, how to write `swarm.toml` for a project, how to read
  `swarm status`. Installed by the repo's install script.
- Install: one script, per role: `install.sh coordinator` / `install.sh worker <coord>`.

## Decided by the owner

- Workers are dynamic, assume 1 h uptime, save hourly; coordinator continues on loss.
- Shutdown is instant: no upload at shutdown; unsaved work is discarded and re-placed elsewhere.
- Swarm cap = 100% − owner's use − 10% for CPU, RAM, GPU; owner's work always primary.
- No AI tokens for decisions: plain formula.
- Projects describe their job once (split / run-resumable / merge).
- Reusable repo under VocanicZ, installable on other devices, many workers.

## Claude's calls (owner may override)

- Name `swarm`; repo `VocanicZ/swarm`, **private**.
- Python 3 standard library only (3.12+ present on both hosts), no pip deps; systemd user units.
- SSH + rsync transport, coordinator-driven; no daemon ports.
- "Adds ≥ 20% this hour" threshold for sending shards.
- Skill now, MCP later.
- First real user: the hg2 power-fit tool (`tools/roblox/power_fit/run.sh`) gets a `swarm.toml`
  as the acceptance test — only when a new fit loop is needed; v4h already passed.

## Left to the builder

Module layout, state file format, exact smoothing constants, log format, test harness (a
localhost "worker" over SSH is fine for end-to-end tests), how `split`/`merge` handle failures.

## Acceptance

1. Unit tests for the cap formula and placement formula.
2. End-to-end: coordinator + one worker on localhost; a toy job of N shards finishes; kill the
   worker mid-run → shards resume on the coordinator from the last save; stopping the worker
   service returns in < 2 s; job output identical to a coordinator-only run.
3. On the owner's PC: start a CPU+GPU stress as "your stuff" → swarm caps drop within ~5 s and
   respect the 10% headroom; stop it → caps recover.

## Addendum (2026-10-25): build route

Owner approved this design, then asked whether existing tools fit. Decision: **do not write the
scheduler from zero and do not fork anything.** Preferred route = **HTCondor (unmodified) + a thin
swarm layer**, conditional on a spike:

| Need | HTCondor piece | swarm layer |
|---|---|---|
| dynamic workers, file transfer, GPU matching | built in | install/join script |
| hourly save, resume elsewhere on loss | self-checkpointing jobs (checkpoint exit code + checkpoint file transfer to submit node) | `swarm.toml` → submit-file translator |
| owner always primary, 100% − use − 10% | jobs run in HTCondor's cgroup slice; **turn off** its suspend/vacate desktop policy | governor sets CPUQuota/MemoryHigh on that slice, stops GPU jobs |
| ≥20% gain rule, tail duplicates | — | small script over condor CLI |
| skill | — | Claude Code skill |

Fallback: if the spike fails or HTCondor setup is too heavy, build this design as written in plain
Python (~1.5–2.5k lines). Forking HTCondor (~1M lines C++) was rejected.
