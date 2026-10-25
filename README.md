<p align="center">
  <img src="docs/logo.svg" alt="swarm logo" width="128" height="128">
</p>

<h1 align="center">swarm</h1>

<p align="center"><em>Spare machines join as workers; the owner's own use always wins.</em></p>

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

Linux with systemd + cgroup v2, Python 3.12+, sudo. Install the coordinator first, then the workers.
HTCondor is installed automatically if missing, and you're prompted once for the pool password (or
pass it with `sudo GET_HTCONDOR_PASSWORD=… bash -s -- …`).

**Worker** (replace `<coord-ip>` with the coordinator's address):

```sh
curl -fsSL https://raw.githubusercontent.com/VocanicZ/swarm/main/install.sh | sudo bash -s -- worker <coord-ip>
```

**Coordinator** (the always-on machine; `--cpu-limit` = the cores swarm may use there):

```sh
curl -fsSL https://raw.githubusercontent.com/VocanicZ/swarm/main/install.sh | sudo bash -s -- coordinator --cpu-limit 32
```

Or use [vocanicz-ai-tools](https://github.com/VocanicZ/vocanicz-ai-tools): `npx github:VocanicZ/vocanicz-ai-tools --setup`
and pick the `swarm` feature.

## Update

```sh
sudo swarm update
```

This fetches the latest `main` and re-runs the install with the role and arguments saved at install
time (`/etc/swarm/install.args`). The installer is idempotent.

## Use

On the coordinator:

```sh
swarm submit path/to/job      # directory with swarm.toml
swarm status [name]           # shards done / running / idle, merge state
swarm cancel <name>
swarm workers                 # machines, state, swarm's current core cap, free GPU memory
swarm wait <name> [--timeout S]   # block until merged: exit 0 done, 1 failed, 124 timeout
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

## From Harness, rtdd and other tools

A tool hands batch work to swarm through the CLI only: write a job dir with a `swarm.toml`, run
`swarm submit <dir>`, then `swarm wait <name>`. The exit code says whether the merged output is
ready. If `swarm` isn't on `PATH`, run the work locally as before. Native support is being added to
[Harness](https://github.com/VocanicZ/Harness) (offloading lane verification) and
[rtdd](https://github.com/VocanicZ/rtdd) (running selected tests as shards).

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
