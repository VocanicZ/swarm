import math
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

from swarm.cli import merge_state
from swarm.jobspec import SpecError

DEFAULT_RETENTION_HOURS = 24.0
REASONS = {"done": "done", "failed": "failed", "pending": "abandoned"}


def check_hours(h: float, what: str) -> float:
    if math.isnan(h) or h < 0:  # nan/negative would make every terminal dir look old enough
        raise SpecError(f"bad {what}: {h!r} hours (want a number >= 0)")
    return h


def retention_hours() -> float:
    """SWARM_JOB_RETENTION_HOURS, else 24; 0 = collect as soon as terminal."""
    v = os.environ.get("SWARM_JOB_RETENTION_HOURS")
    try:
        h = float(v) if v not in (None, "") else DEFAULT_RETENTION_HOURS
    except ValueError:
        raise SpecError(f"bad SWARM_JOB_RETENTION_HOURS: {v!r} hours (want a number >= 0)") from None
    return check_hours(h, "SWARM_JOB_RETENTION_HOURS")


def queued_names() -> set[str]:
    try:
        q = subprocess.run(["condor_q", "-af", "JobBatchName"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SpecError(f"condor_q failed, cannot tell what is queued; collecting nothing: {e}") from e
    if q.returncode:  # schedd down: empty output must not read as "nothing queued"
        raise SpecError(f"condor_q failed (exit {q.returncode}), cannot tell what is queued; collecting nothing: "
                        f"{q.stderr.strip()}")
    return set(q.stdout.split())


def _scan(d: Path) -> tuple[float, int]:
    """(newest mtime, total bytes) of d and everything under it, never following symlinks."""
    newest, size = os.lstat(d).st_mtime, 0
    for top, dirs, files in os.walk(d):  # followlinks=False: symlinked dirs are listed, not entered
        for n in dirs + files:
            st = os.lstat(os.path.join(top, n))
            newest = max(newest, st.st_mtime)
            if not stat.S_ISDIR(st.st_mode):
                size += st.st_size
    return newest, size


def _is_job_dir(d: Path) -> bool:
    try:
        if not stat.S_ISDIR(os.lstat(d).st_mode):  # symlinks and plain files are never ours
            return False
    except OSError:
        return False
    return os.path.lexists(d / "job.json") or os.path.lexists(d / "job.dag")


def collect(state_root: Path, older_than_hours: float | None, dry_run: bool = False,
            queued: set[str] | None = None, now: float | None = None,
            skipped: list[tuple[str, str]] | None = None) -> list[tuple[str, str, int]]:
    """Remove terminal job state dirs idle for older_than_hours (None = any age).

    Returns (name, reason, bytes) per dir removed, or that would be removed with dry_run.
    A dir that cannot be read or removed is left, appended to skipped as (name, error), and the
    pass goes on. Raises SpecError, collecting nothing, when condor_q cannot say what is queued."""
    if queued is None:
        queued = queued_names()
    now = time.time() if now is None else now
    root = Path(state_root)
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        # <name>.prev is a superseded run, never itself queued: its own age decides, not its base's state
        if (not d.name.endswith(".prev") and d.name in queued) or not _is_job_dir(d):
            continue
        try:
            newest, size = _scan(d)
            if older_than_hours is not None and now - newest < older_than_hours * 3600:
                continue
            reason = REASONS[merge_state(d)]
            if not dry_run:
                shutil.rmtree(d)
        except (OSError, ValueError) as e:  # one bad dir (binary merge.log, read-only subdir, race) must not stop the pass
            if skipped is not None:
                skipped.append((d.name, str(e)))
            continue
        out.append((d.name, reason, size))
    return out


def human(n: float) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def report(entries, dry_run: bool, skipped=()) -> str:
    verb = "would remove" if dry_run else "removed"
    lines = [f"{verb} {name}: {reason}, {human(size)}" for name, reason, size in entries]
    lines += [f"skipped {name}: {err}" for name, err in skipped]
    lines.append(f"total: {verb} {len(entries)} job(s), {human(sum(e[2] for e in entries))}")
    return "\n".join(lines)
