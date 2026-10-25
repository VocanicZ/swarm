# Handoff — 2026-10-25

**State:** design approved by the owner (`docs/superpowers/specs/2026-10-25-swarm-design.md`,
read the Addendum at the bottom). Nothing built yet.

## Next step: 1-hour HTCondor spike (throwaway; owner said yes)

Goal: prove HTCondor's self-checkpointing + resume-elsewhere works between the two machines.

1. Install HTCondor 25.x (official apt repo) on:
   - **coordinator** = claude machine: Proxmox LXC 9901 on pvebig, `ssh claude` (Tailscale
     100.119.103.87, LAN 192.168.1.111). Debian, Python 3.13, 64 threads, Proxmox cpulimit 32
     (owner rule: never raise). No GPU. `claude` user has no passwordless sudo: get root via
     `ssh root@192.168.1.100 'pct exec 9901 -- …'`. Runs Harness fleets — do not disturb them.
   - **worker** = owner's desktop (this repo's machine, Ubuntu, 20 threads, 125 GB, RTX 3060 Ti 8 GB,
     LAN 192.168.1.123 on a 100 Mb/s link). Needs owner's sudo: ask them to run commands with `! `.
   Role: claude = central manager + submit + execute; desktop = execute only.
2. Toy job: counts to N, writes progress file atomically every ~minute, exits with the checkpoint
   exit code; submit with checkpoint file transfer. Verify with current HTCondor docs that the
   feature names are right (not verified yet).
3. Kill the desktop worker mid-run (stop condor service / pull network). Pass = job resumes on
   claude from the last checkpoint, not from zero.
4. Check: jobs land in a cgroup slice on the desktop whose CPUQuota/MemoryHigh we can set at runtime
   with the desktop policy (suspend/vacate) disabled.
5. Report to owner: pass/fail + how heavy the setup was → choose route (HTCondor + thin layer, or
   own build per the design). Then `superpowers:writing-plans`.

## Owner rules to keep
- Owner's own use always wins: swarm cap = 100% − owner use − 10% (CPU, RAM, GPU separately).
- Zero AI tokens in any control loop.
- Shutdown is instant: no upload; unsaved work discarded and re-placed elsewhere.
- Assume a worker is up for 1 hour; save hourly.
- Shards run on workers as a dedicated `swarm` user, never as the owner.
