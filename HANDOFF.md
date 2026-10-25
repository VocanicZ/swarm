# Handoff — 2026-10-25

**State (2026-10-25):** v1 built on branch `v1-layer` (plan `docs/superpowers/plans/2026-10-25-swarm-htcondor-layer.md`,
all 8 tasks reviewed; final whole-branch review fixes applied) and **deployed live**:
- coordinator = claude (LXC 9901): installed from a root-owned snapshot (`/root/swarm-src`, `install.sh coordinator --cpu-limit 32`).
  NUM_CPUS 32, MEMORY 65536, condor.service CPUWeight=idle, jobs nice 19, `swarm-tick.timer` every 60 s, skill in
  `~claude/.claude/skills/swarm`. Redeploy: `git archive` HEAD → pct push → `install.sh coordinator --cpu-limit 32` (see git log).
- worker = desktop: `sudo bash ~/swarm/install.sh worker 10.0.0.1` (owner sudo). Governor `swarm-governor.service`,
  shards run as `swarm`, `systemctl stop condor` is instant.

**Acceptance (live):** kill test PASS (stop 0.015 s; desktop shards resumed on claude from last checkpoint; merged output
identical). CPU caps PASS (12 → 5 cores within 3 s under owner load; recover after 30 s calm). GPU: VRAM tracking PASS;
high-utilisation stop path **not yet exercised** (try with a game: `/run/swarm/gpu.json` should go `ok: false` in ~5 s).

## Open items
- (done) v1-layer merged to main 2026-10-25.
- Untested risk: condor.service CPUWeight=idle on claude also idles the schedd/collector; under full Harness load
  worker leases (180 s) might lapse → watch `NumJobStarts` on worker shards when claude is saturated.
- Deferred (v1): tail duplicates, measured link rate, output bytes in move cost; `build()` double-failure can lose `.prev`.
- First real user: hg2 power-fit `swarm.toml` when a new fit loop is needed.

## Owner rules to keep
- Owner's own use always wins: swarm cap = 100% − owner use − 10% (CPU, RAM, GPU separately).
- Zero AI tokens in any control loop.
- Shutdown is instant: no upload; unsaved work discarded and re-placed elsewhere.
- Assume a worker is up for 1 hour; save hourly.
- Shards run on workers as a dedicated `swarm` user, never as the owner.
