# swarm

Spare Linux machines become workers for an always-on coordinator. A project describes a job once in
`swarm.toml` (split, resumable run, merge). Its shards then run across the coordinator and any
worker that is up. The worker owner's own use always comes first: swarm takes at most
**100% − owner use − 10%** of CPU, RAM and GPU, re-checked every 2 s. Stopping a worker is
instant. A lost worker costs at most the work since its last hourly save; its shards resume
elsewhere from that save.

Built on [HTCondor](https://htcondor.org) 25.x (unmodified). swarm is a thin standard-library
Python layer over it: a submit translator, a shard wrapper, a worker governor and a coordinator
tick. No pip dependencies, and no AI calls in any control loop.

## Install

Linux with systemd + cgroup v2, Python 3.12+. Run as a user with sudo. Install the coordinator
first, then the workers. HTCondor is installed automatically if missing, and you're prompted once
for the pool password (or set `GET_HTCONDOR_PASSWORD`).

**Worker** (replace `<coord-ip>` with the coordinator's address):

```sh
git clone https://github.com/VocanicZ/swarm.git ~/swarm && sudo bash ~/swarm/install.sh worker <coord-ip>
```

**Coordinator** (always-on machine; `--cpu-limit` = the cores swarm may use there):

```sh
sudo git clone https://github.com/VocanicZ/swarm.git /root/swarm-src && sudo bash /root/swarm-src/install.sh coordinator --cpu-limit 32
```

On the coordinator, clone as root so that no agent-writable directory feeds code to the root
services.

## Update

**Worker:**

```sh
git -C ~/swarm pull && sudo bash ~/swarm/install.sh worker <coord-ip>
```

**Coordinator:**

```sh
sudo git -C /root/swarm-src pull && sudo bash /root/swarm-src/install.sh coordinator --cpu-limit 32
```

`install.sh` is idempotent. It re-syncs `/opt/swarm`, rewrites the configs and restarts the
services.

## Use

On the coordinator:

```sh
swarm submit path/to/job      # directory with swarm.toml
swarm status [name]           # shards done / running / idle, merge state
swarm cancel <name>
swarm workers                 # machines, state, swarm's current core cap, free GPU memory
```

Minimal `swarm.toml`:

```toml
name  = "my-job"
split = "tools/split.sh"                 # prints one shard id per line
run   = "tools/run.sh {shard} {out}"     # must resume from whatever is in {out}
merge = "tools/merge.sh {outs}"          # runs on the coordinator when every shard is done
# optional: workdir, threads, mem_mb, gpu = "no|optional|required", vram_mb, setup, check, save_seconds
```

`run` writes progress into `{out}` at least every 30 min and writes each file atomically (temp file,
then rename). Full contract: [`skill/SKILL.md`](skill/SKILL.md), which is also installed as the
`swarm` Claude Code skill on the coordinator.

On a worker: `sudo systemctl stop condor` stops it instantly. Nothing is uploaded, and its shards
resume elsewhere from their last save. `swarm leave` also disables it at boot.

## Layout

| Path | What |
|---|---|
| `swarm/jobspec.py` | `swarm.toml` loader and validator |
| `swarm/submit.py`, `swarm/cli.py` | `swarm.toml` → HTCondor DAG (shards + merge); CLI |
| `swarm/shard.py` | shard wrapper: hourly checkpoint (exit 85), GPU stop/start, check/setup |
| `swarm/governor.py` | worker governor: cgroup caps, publishes `Swarm*` machine attrs |
| `swarm/tick.py` | coordinator tick: drops dead workers, publishes coordinator free cores |
| `condor/`, `systemd/`, `install.sh` | HTCondor config templates, units, installer |
| `docs/superpowers/` | design spec and implementation plan |
| `spike/RESULTS.md` | HTCondor spike results and gotchas |

Tests: `python3 -m unittest`. The live end-to-end test is `tests/e2e/e2e.sh`, run on the coordinator.
