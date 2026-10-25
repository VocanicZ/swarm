import hashlib
import json
import math
import os
import shlex
import shutil
import subprocess
from pathlib import Path

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
    shutil.rmtree(statedir, ignore_errors=True)
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
    sh.write_text(f"#!/bin/sh\ncd {shlex.quote(str(spec.jobdir))} && exec {merge}\n")
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


def submit(jobdir: Path, state_root: Path = Path.home() / ".swarm/jobs") -> str:
    spec = load(jobdir)
    shards = split(spec)
    q = subprocess.run(["condor_q", "-af", "JobBatchName"], capture_output=True, text=True)
    if spec.name in q.stdout.split():
        raise SpecError(f"job {spec.name!r} already in the queue")
    statedir = Path(state_root) / spec.name
    build(spec, shards, statedir)
    subprocess.run(["condor_submit_dag", "-batch-name", spec.name, "job.dag"], cwd=statedir, check=True)
    return spec.name
