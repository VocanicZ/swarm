import io
import re
import unittest
from contextlib import redirect_stdout, suppress
from pathlib import Path

from swarm import cli, gc

REPO = Path(__file__).resolve().parent.parent


def gc_flags():
    out = io.StringIO()
    with redirect_stdout(out), suppress(SystemExit):
        cli.main(["gc", "--help"])
    return set(re.findall(r"--[a-z][a-z-]+", out.getvalue())) - {"--help"}


def section(text, heading):
    m = re.search(rf"^## {heading}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return m.group(1) if m else ""


class T(unittest.TestCase):
    def test_readme_documents_job_state_retention(self):
        s = section((REPO / "README.md").read_text(), "Job state and `swarm gc`")
        self.assertTrue(s, "README has no job state retention section")
        flags = gc_flags()
        self.assertEqual(flags, {"--dry-run", "--older-than", "--all-terminal"})
        for x in [*flags, "~/.swarm/jobs", "SWARM_JOB_RETENTION_HOURS", f"{gc.DEFAULT_RETENTION_HOURS:.0f} h",
                  "`0`", ".prev", "swarm submit", "swarm-tick", "--dry-run", "total: would remove"]:
            self.assertIn(x, s)
        for reason in gc.REASONS.values():
            self.assertIn(reason, s)

    def test_skill_says_merge_copies_results_out(self):
        s = (REPO / "skill/SKILL.md").read_text()
        self.assertRegex(s, r"(?s)`merge` must copy.{0,200}out of the state dir")
        self.assertIn("garbage-collected", s)
