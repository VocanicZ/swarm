import io
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from swarm import cli, gc
from swarm.jobspec import SpecError

OK = "005 (12.000.000) 2026-10-25 10:00:00 Job terminated.\n\t(1) Normal termination (return value 0)\n"
BAD = "005 (12.000.000) 2026-10-25 10:00:00 Job terminated.\n\t(1) Normal termination (return value 3)\n"
HOUR = 3600


def q(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, "schedd down" if returncode else "")


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop("SWARM_JOB_RETENTION_HOURS", None)

    def job(self, name, merge_log=None, age_h=48, marker="job.json"):
        d = self.root / name
        (d / "shards/a/out").mkdir(parents=True)
        (d / marker).write_text("{}")
        (d / "shards/a/out/x").write_text("x" * 100)
        if merge_log is not None:
            (d / "merge.log").write_text(merge_log)
        self.age(d, age_h)
        return d

    def age(self, d, hours):
        t = time.time() - hours * HOUR
        for top, dirs, files in os.walk(d):
            for n in dirs + files:
                os.utime(os.path.join(top, n), (t, t), follow_symlinks=False)
        os.utime(d, (t, t))


class Collect(Base):
    def collect(self, hours=24, queued="", returncode=0, dry_run=False):
        with mock.patch("swarm.gc.subprocess.run", return_value=q(queued, returncode)) as run:
            out = gc.collect(self.root, hours, dry_run)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["condor_q", "-af", "JobBatchName"])
        return out

    def test_queued_job_kept(self):
        d = self.job("j", OK)
        self.assertEqual(self.collect(queued="other\nj\n"), [])
        self.assertTrue(d.exists())

    def test_done_and_failed_kept_inside_window(self):
        a, b = self.job("a", OK, age_h=1), self.job("b", BAD, age_h=1)
        self.assertEqual(self.collect(), [])
        self.assertTrue(a.exists() and b.exists())

    def test_done_and_failed_collected_after_window(self):
        a, b = self.job("a", OK), self.job("b", BAD)
        out = self.collect()
        self.assertEqual([(n, r) for n, r, _ in out], [("a", "done"), ("b", "failed")])
        self.assertFalse(a.exists() or b.exists())
        self.assertTrue(all(s >= 100 for _, _, s in out))

    def test_one_recent_file_keeps_job(self):
        d = self.job("a", OK)
        os.utime(d / "shards/a/out/x")  # newest mtime under the dir, not the dir's own, decides
        self.assertEqual(self.collect(), [])

    def test_pending_out_of_queue_is_abandoned(self):
        d = self.job("p", marker="job.dag")
        self.assertEqual([(n, r) for n, r, _ in self.collect()], [("p", "abandoned")])
        self.assertFalse(d.exists())

    def test_condor_q_failure_collects_nothing(self):
        d = self.job("a", OK)
        with self.assertRaises(SpecError):
            self.collect(returncode=1)
        with mock.patch("swarm.gc.subprocess.run", side_effect=subprocess.TimeoutExpired("condor_q", 60)), \
                self.assertRaises(SpecError):
            gc.collect(self.root, 24)
        self.assertTrue(d.exists())

    def test_dry_run_deletes_nothing(self):
        d = self.job("a", OK)
        self.assertEqual([(n, r) for n, r, _ in self.collect(dry_run=True)], [("a", "done")])
        self.assertTrue(d.exists())

    def test_non_swarm_dir_skipped(self):
        d = self.root / "mine"
        d.mkdir()
        (d / "notes").write_text("keep")
        self.age(d, 48)
        self.assertEqual(self.collect(), [])
        self.assertTrue(d.exists())

    def test_prev_skipped(self):
        d = self.job("a.prev", OK)
        self.assertEqual(self.collect(), [])
        self.assertTrue(d.exists())

    def test_symlink_in_root_not_followed(self):
        outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (outside / "job.json").write_text("{}")
        (outside / "merge.log").write_text(OK)
        (self.root / "link").symlink_to(outside)
        self.age(outside, 48)
        self.assertEqual(self.collect(hours=None), [])
        self.assertTrue((outside / "job.json").exists())

    def test_symlink_inside_job_not_followed(self):
        outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (outside / "big").write_text("y" * 10000)
        d = self.job("a", OK)
        (d / "ln").symlink_to(outside)
        self.age(d, 48)
        os.utime(outside / "big")  # fresh outside the root: must not keep the job alive
        out = self.collect()
        self.assertEqual([n for n, _, _ in out], ["a"])
        self.assertLess(out[0][2], 10000)
        self.assertTrue((outside / "big").exists())

    def test_no_window_collects_any_age(self):
        self.job("a", OK, age_h=0)
        self.assertEqual([n for n, _, _ in self.collect(hours=None)], ["a"])

    def test_retention_env(self):
        self.assertEqual(gc.retention_hours(), 24)
        os.environ["SWARM_JOB_RETENTION_HOURS"] = "0"
        self.assertEqual(gc.retention_hours(), 0)
        self.job("a", OK, age_h=0)
        self.assertEqual([n for n, _, _ in self.collect(hours=gc.retention_hours())], ["a"])

    def test_missing_root(self):
        with mock.patch("swarm.gc.subprocess.run", return_value=q()):
            self.assertEqual(gc.collect(self.root / "nope", 24), [])


class Human(unittest.TestCase):
    def test_human(self):
        self.assertEqual([gc.human(n) for n in (0, 1023, 1536, 5 * 1024**2, 3 * 1024**3, 2048 * 1024**3)],
                         ["0B", "1023B", "1.5K", "5.0M", "3.0G", "2048.0G"])


class Cli(Base):
    def main(self, *argv, queued="", returncode=0):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "ROOT", self.root), \
                mock.patch("swarm.gc.subprocess.run", return_value=q(queued, returncode)), \
                redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["gc", *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_output_lines_and_total(self):
        self.job("a", OK)
        self.job("p")
        rc, out, _ = self.main()
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^removed a: done, \d+(\.\d)?[BKMG]$")
        self.assertRegex(lines[1], r"^removed p: abandoned, ")
        self.assertRegex(lines[2], r"^total: removed 2 job\(s\), ")

    def test_dry_run_prints_same_lines(self):
        self.job("a", OK)
        _, dry, _ = self.main("--dry-run")
        self.assertTrue((self.root / "a").exists())
        _, real, _ = self.main()
        self.assertEqual(dry.replace("would remove", "removed"), real)
        self.assertFalse((self.root / "a").exists())

    def test_older_than_overrides_env(self):
        os.environ["SWARM_JOB_RETENTION_HOURS"] = "100"
        self.job("a", OK, age_h=3)
        self.assertIn("total: removed 0", self.main()[1])
        self.assertIn("removed a: done", self.main("--older-than", "2")[1])

    def test_env_zero_collects_immediately(self):
        os.environ["SWARM_JOB_RETENTION_HOURS"] = "0"
        self.job("a", OK, age_h=0)
        self.assertIn("removed a: done", self.main()[1])

    def test_all_terminal_ignores_window(self):
        self.job("a", OK, age_h=0)
        self.assertIn("total: removed 0", self.main()[1])
        self.assertIn("removed a: done", self.main("--all-terminal")[1])

    def test_condor_q_failure_exit_2(self):
        self.job("a", OK)
        rc, out, err = self.main(returncode=1)
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("condor_q failed", err)
        self.assertTrue((self.root / "a").exists())

    def test_listed_in_help(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--help"])
        self.assertIn("gc", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["gc", "--help"])
        for flag in ("--dry-run", "--older-than", "--all-terminal"):
            self.assertIn(flag, out.getvalue())


if __name__ == "__main__":
    unittest.main()
