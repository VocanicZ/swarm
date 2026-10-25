# HTCondor spike — 2026-10-25: PASS

Pool: claude (LXC 9901, 10.0.0.1) = central manager + submit + execute (`NUM_CPUS = 2` for the
spike); desktop (10.0.0.2) = execute. HTCondor 25.14.1 from `get.htcondor.org` on both
(Debian 13 trixie, Ubuntu 24.04 noble are supported).

## Results

| Test | Result |
|---|---|
| Install | One installer command per machine plus a small config file. The desktop install hit an unrelated broken WineHQ apt key. |
| Self-checkpoint (`checkpoint_exit_code = 85`, `transfer_checkpoint_files`) | Each checkpoint was saved to claude's spool right away (`/var/spool/condor/<cluster>/0/…/count.ckpt`). |
| Desktop killed by SIGKILL mid-run (power-loss stand-in) | Job 5 restarted on claude from the last saved checkpoint (180), not from 0. It finished at 600 with exit code 0 after 2 job starts. |
| Desktop policy | The execute role's defaults already have `SUSPEND = False`, `PREEMPT = False`, `START = True`, so nothing needed turning off. |
| Runtime cgroup caps | Jobs run in `system.slice/condor.service/htcondor/<slot>.scope`. Writing `cpu.max` / `memory.high` on `…/condor.service/htcondor` while a 4-core job ran gave 4.00 → 1.52 → 4.00 cores and the job kept running. |

## Gotchas the swarm layer must handle (all config or small scripts, no forks)

1. **`NEGOTIATOR_PRE_JOB_RANK` default** has `- 100000 * Cpus`. It packs jobs onto the smallest
   slot and overrides the job's `rank`. We set
   `(10000000 * My.Rank) + (1000000 * (RemoteOwner =?= UNDEFINED))`; swarm must own this ranking.
2. **`JobLeaseDuration` default is 2400 s**, so a dead worker holds the job for 40 minutes. Use
   `job_lease_duration = 120` (or lower) in every submit file.
3. **Stale startd ads**: the collector keeps a dead worker's ads (UPDATE_INTERVAL 300 s → ads live
   ~15 min). The negotiator kept re-matching the job to the dead desktop because of its rank, and
   each claim failed. Fix: a liveness loop on the coordinator that runs
   `condor_advertise INVALIDATE_STARTD_ADS` (`Requirements = Machine == "<host>"`) when a worker
   stops answering, then `condor_reschedule`. A shorter UPDATE_INTERVAL helps too.
4. **Submitter ad lag**: after an eviction the schedd reported `IdleJobs = 0` until its next
   `SCHEDD_INTERVAL` (300 s). Run `condor_reschedule` after invalidating ads (point 3), or lower
   `SCHEDD_INTERVAL`.
5. Jobs run as `nobody`. The design wants a dedicated `swarm` user: create the user and map slots
   to it (e.g. `SLOT<N>_USER` / `DEDICATED_EXECUTE_ACCOUNT_REGEXP`; check the docs).
6. `rank` / `requirements` expressions must use `TARGET.Machine`; a bare `Machine` resolves
   against the job ad and comes out undefined.
7. The desktop has many network interfaces (docker, tailscale, wifi), so pin `NETWORK_INTERFACE`.

## Verdict

Choose route **HTCondor unmodified + thin swarm layer**. Setup is light. Gotchas 1–4 fit in the
governor/liveness script plus the submit-file translator.
