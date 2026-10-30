import hashlib
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from swarm import gc
from swarm.jobspec import SHARD_RE, JobSpec, SpecError, load

SHARD_PY = "/opt/swarm/swarm/shard.py"


def split(spec: JobSpec) -> list[str]:
    r = subprocess.run(["sh", "-c", spec.split], cwd=spec.jobdir, capture_output=True, text=True)
    if r.returncode:
        raise SpecError(f"split failed (exit {r.returncode}): {r.stderr.strip()}")
    ids = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    if not ids:
        raise SpecError("split produced no shards")
    seen = set()
    for i in ids:
        if not SHARD_RE.fullmatch(i):
            raise SpecError(f"bad shard id: {i!r}")
        if i in seen:
            raise SpecError(f"duplicate shard id: {i!r}")
        seen.add(i)
    return ids


def build(spec: JobSpec, shards: list[str], statedir: Path) -> Path:
    statedir = Path(statedir)
    if statedir.exists():  # keep the last run (incl. finished outs) one generation back
        prev = statedir.with_name(statedir.name + ".prev")
        if prev.exists():
            shutil.rmtree(prev)
        statedir.rename(prev)
    statedir.mkdir(parents=True)
    tarball = statedir / "workdir.tar.gz"
    cmd = ["tar", "-czf", str(tarball)]
    ignore = spec.workdir / ".swarmignore"
    if ignore.exists():
        cmd.append(f"--exclude-from={ignore}")
    subprocess.run(cmd + ["-C", str(spec.workdir), "."], check=True)
    data = tarball.read_bytes()
    (statedir / "job.json").write_text(json.dumps({
        "run": spec.run, "check": spec.check, "setup": spec.setup, "gpu": spec.gpu,
        "vram_mb": spec.vram_mb, "save_seconds": spec.save_seconds,
        "version": hashlib.sha256(data).hexdigest()[:16],
    }))
    (statedir / "shards.txt").write_text("".join(f"{i}\n" for i in shards))
    for i in shards:
        (statedir / "shards" / i).mkdir(parents=True)
    outs = " ".join(shlex.quote(str(statedir / "shards" / i / "out")) for i in shards)
    merge = spec.merge.replace("{outs}", outs)
    sh = statedir / "merge.sh"
    sh.write_text(f"#!/bin/sh\nexport PATH\ncd {shlex.quote(str(spec.jobdir))} && {merge}\n")  # job env has no PATH
    sh.chmod(0o755)
    (statedir / "job.dag").write_text(
        "JOB shards shards.sub\nJOB merge merge.sub\nPARENT shards CHILD merge\n")
    (statedir / "merge.sub").write_text(
        "universe   = local\nexecutable = merge.sh\n"
        "output     = merge.out\nerror      = merge.err\nlog        = merge.log\nqueue\n")
    if spec.gpu == "required":
        req = f" && TARGET.SwarmGpus >= 1 && TARGET.SwarmGpuFreeMB >= {spec.vram_mb}"
        rank = "0"
    elif spec.gpu == "optional":
        req, rank = "", f"ifThenElse(TARGET.SwarmGpuFreeMB >= {spec.vram_mb}, 1, 0)"
    else:
        req, rank = "", "0"
    mb = len(data) / 1e6
    s = str(statedir)
    (statedir / "shards.sub").write_text(f"""\
executable                = {os.environ.get("SWARM_SHARD_PY", SHARD_PY)}
arguments                 = $(shard)
initialdir                = shards/$(shard)
transfer_input_files      = {s}/workdir.tar.gz, {s}/job.json
should_transfer_files     = yes
checkpoint_exit_code      = 85
transfer_checkpoint_files = out
transfer_output_files     = out
job_lease_duration        = 180
max_retries               = 3
job_machine_attrs         = Machine
requirements              = (MY.ExitCode =!= 86 || TARGET.Machine =!= MY.MachineAttrMachine0){req}
rank                      = {rank}
request_cpus              = {spec.threads}
request_memory            = {spec.mem_mb}
request_disk              = {2 * math.ceil(mb) + 1024}M
+SwarmJob                 = "{spec.name}"
+SwarmInputMB             = {mb:.3f}
+SwarmGpu                 = "{spec.gpu}"
output = run.out
error  = run.err
log    = {s}/shards.log
queue shard from shards.txt
""")
    return statedir / "job.dag"


def collect_garbage(state_root: Path, keep: str) -> None:
    """Best-effort `swarm gc` before a submit; never fails it. keep (the job being submitted) is left
    for build() to rotate to .prev rather than collected."""
    skipped = []
    try:
        gc.collect(state_root, gc.retention_hours(), queued=gc.queued_names() | {keep}, skipped=skipped)
    except Exception as e:
        print(f"swarm: gc skipped: {e}", file=sys.stderr)
    for name, err in skipped:
        print(f"swarm: gc skipped {name}: {err}", file=sys.stderr)


def submit(jobdir: Path, state_root: Path = Path.home() / ".swarm/jobs") -> str:
    spec = load(jobdir)
    collect_garbage(state_root, spec.name)
    shards = split(spec)
    try:
        q = subprocess.run(["condor_q", "-af", "JobBatchName"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SpecError(f"condor_q failed, cannot tell if {spec.name!r} is queued: {e}") from e
    if q.returncode:  # schedd down: empty output must not read as "not queued"
        raise SpecError(f"condor_q failed (exit {q.returncode}), cannot tell if {spec.name!r} is queued: "
                        f"{q.stderr.strip()}")
    if spec.name in q.stdout.split():
        raise SpecError(f"job {spec.name!r} already in the queue")
    statedir = Path(state_root) / spec.name
    build(spec, shards, statedir)
    subprocess.run(["condor_submit_dag", "-batch-name", spec.name, "job.dag"], cwd=statedir, check=True)
    return spec.name
