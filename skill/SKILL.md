---
name: swarm
description: Run CPU/GPU-heavy batch work across spare machines via `swarm submit`. Use when a job splits into independent resumable shards (parameter sweeps, fits, renders, simulations, bulk conversions) that would take more than ~30 min on one machine. Covers writing `swarm.toml`, the resumable `run` contract, and reading `swarm status`. Not for agent sessions, servers, or anything interactive.
---

# swarm

swarm runs a job's shards on the coordinator (idle priority, never above its CPU cap) and on spare
Linux workers. The owner's own use of a worker always wins: shards get capped, stopped, or moved
at any moment. Placement is a plain formula over HTCondor; it costs no AI tokens.

## When to use

- Use it for long work that splits into **independent** shards, each finishing in about 5–30 min,
  where each shard can **resume** from its own saved state.
- Don't use it for agent sessions, long-running servers, interactive tools, work whose shards need
  to talk to each other, or anything that needs the owner's files or credentials at run time.

## The job: a directory with `swarm.toml`

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
mem_mb  = 1024                     # optional: RAM per shard (default 1024)
save_seconds = 3600                # optional: checkpoint interval (default 3600)
```

- **Required:** `name`, `split`, `run` (must contain `{out}`), and `merge` (must contain `{outs}`).
  Any other key is rejected.
- **Defaults:** `workdir="."`, `gpu="no"`, `threads=1`, `mem_mb=1024`, `save_seconds=3600`.
  `vram_mb` is required when `gpu` is not `"no"`.
- `name` must match `[A-Za-z0-9][A-Za-z0-9._-]*`. Each shard id printed by `split` must match
  `[A-Za-z0-9._-]+`, and ids must be unique. Otherwise `swarm submit` fails.
- `workdir` is packed into a tarball **at submit time**, honouring `.swarmignore`, which is a tar
  exclude list. Edits made after submitting do not reach running shards. Keep it small: a large
  input makes slow-link workers lose the placement formula.
- `split` and `merge` run in the job dir on the coordinator. `{outs}` is the space-separated list
  of every shard's `out` dir, in `split` order.
- `run`, `check`, and `setup` run with the cwd at the unpacked copy of `workdir` on the machine
  running the shard.
  - On a worker that is the `swarm` user, which cannot read the owner's home. Use relative paths
    inside `workdir` and never absolute paths to the owner's files.
  - `check` runs before shards. If it fails, `setup` runs (once per job version), and `check`
    runs again. A second failure marks the machine unfit, and the shard goes elsewhere.

## Rules for `run`

1. **The only state is `{out}`.** On every start, look in `{out}` and continue from what is there.
   Never assume a fresh start, and never keep state anywhere else.
2. **Write progress into `{out}` at least every 30 min.** Every `save_seconds` the wrapper waits
   for the next write to `{out}`, then stops `run` (SIGTERM, then SIGKILL 10 s later) and saves
   `{out}`. The shard restarts later, possibly on another machine. If nothing is written, the
   wrapper stops `run` anyway at 1.5 × `save_seconds`.
3. **Write each file atomically:** write a temp file in `{out}`, then `mv` it over the real name.
   A copy taken at any moment must be consistent.
4. **Honour `SWARM_GPU`.** `SWARM_GPU=1` (with `CUDA_VISIBLE_DEVICES=0`) means you may use the
   GPU. `SWARM_GPU=0` (with `CUDA_VISIBLE_DEVICES=""`) means CPU only.
   - With `gpu="optional"`, one shard may switch between the two modes across restarts, so its
     state in `{out}` must work for both.
   - The GPU can be taken away at any moment, for example when the owner starts a game. Then
     `run` is killed and restarted from `{out}`.
5. Use `threads` threads, not `nproc`.
6. Exit 0 when the shard is complete. Never exit 85 or 86, which are reserved for checkpoint and
   unfit. Any other non-zero exit is retried up to 3 times.

## CLI

| Command | Effect |
|---|---|
| `swarm submit <jobdir>` | Validate, split, pack, and queue. Refused while the job's name is still queued. A resubmit **replaces** the old state dir in `~/.swarm/jobs/<name>/`, including its outs. |
| `swarm status [name]` | Show progress for all jobs, or for one. |
| `swarm cancel <name>` | Remove all of the job's queued and running shards and the merge. |
| `swarm workers` | Print one row per slot: `Machine State Cpus SwarmCapCores SwarmGpuFreeMB`. |
| `swarm join <coord-ip> [--link-mbps N]` / `swarm leave` | Add this machine as a worker, or remove it (needs sudo; owner only). |

Reading `swarm status`:

```
hg2-power-fit-v4i: 37/200 done, 12 running (claude, NathanielSong), 151 idle
  merge: pending
```

- `done` counts shards whose `out` is back on the coordinator. `running` lists the hosts that
  have shards now. `idle` is queued, waiting for a slot that passes the placement formula.
- `merge: done` means the merge ran. Its output is in the job dir, and its logs are in
  `~/.swarm/jobs/<name>/merge.{out,err}`.
- A shard's stdout and stderr are in `~/.swarm/jobs/<name>/shards/<id>/run.{out,err}`.
- If shards stay idle, check `swarm workers`. A worker whose `SwarmCapCores` is 0 or `undefined`
  is busy with the owner's work or has no governor. With `gpu="required"`, shards wait for a GPU
  worker with enough free VRAM.
