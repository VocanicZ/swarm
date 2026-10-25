import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHARD_RE = re.compile(r"^[A-Za-z0-9._-]+$")

REQUIRED = ("name", "split", "run", "merge")
OPTIONAL = ("workdir", "gpu", "vram_mb", "threads", "mem_mb", "setup", "check", "save_seconds")


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class JobSpec:
    name: str
    jobdir: Path
    workdir: Path
    gpu: str
    vram_mb: int
    threads: int
    mem_mb: int
    setup: str | None
    check: str | None
    split: str
    run: str
    merge: str
    save_seconds: int


def _int(d, key, default, minimum):
    v = d.get(key, default)
    if type(v) is not int or v < minimum:
        raise SpecError(f"{key} must be an integer >= {minimum}")
    return v


def _str(d, key):
    v = d.get(key)
    if v is not None and (not isinstance(v, str) or not v):
        raise SpecError(f"{key} must be a non-empty string")
    return v


def load(jobdir: Path) -> JobSpec:
    jobdir = Path(jobdir).resolve()
    try:
        d = tomllib.loads((jobdir / "swarm.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SpecError(f"cannot read swarm.toml: {e}") from e
    unknown = set(d) - set(REQUIRED) - set(OPTIONAL)
    if unknown:
        raise SpecError(f"unknown keys: {', '.join(sorted(unknown))}")
    for k in REQUIRED:
        if _str(d, k) is None:
            raise SpecError(f"missing required key: {k}")
    if not NAME_RE.match(d["name"]):
        raise SpecError(f"bad name: {d['name']!r}")
    if "{out}" not in d["run"]:
        raise SpecError("run must contain {out}")
    if "{outs}" not in d["merge"]:
        raise SpecError("merge must contain {outs}")
    gpu = d.get("gpu", "no")
    if gpu not in ("no", "optional", "required"):
        raise SpecError(f"gpu must be no|optional|required, got {gpu!r}")
    if gpu != "no" and "vram_mb" not in d:
        raise SpecError("gpu != 'no' requires vram_mb")
    workdir = (jobdir / (_str(d, "workdir") or ".")).resolve()
    if not workdir.is_dir():
        raise SpecError(f"workdir not found: {workdir}")
    return JobSpec(
        name=d["name"], jobdir=jobdir, workdir=workdir, gpu=gpu,
        vram_mb=_int(d, "vram_mb", 0, 0), threads=_int(d, "threads", 1, 1),
        mem_mb=_int(d, "mem_mb", 1024, 1), setup=_str(d, "setup"), check=_str(d, "check"),
        split=d["split"], run=d["run"], merge=d["merge"],
        save_seconds=_int(d, "save_seconds", 3600, 1),
    )
