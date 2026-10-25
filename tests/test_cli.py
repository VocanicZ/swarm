import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from swarm import cli

# fixture lines in the formats HTCondor 25 writes (user log / DAGMan .dagman.out)
OK = "005 (12.000.000) 2026-10-25 10:00:00 Job terminated.\n\t(1) Normal termination (return value 0)\n"
BAD = "005 (12.000.000) 2026-10-25 10:00:00 Job terminated.\n\t(1) Normal termination (return value 3)\n"
DAG_FAIL = "10/25/26 10:00:00 **** condor_dagman (condor_DAGMAN) pid 4242 EXITING WITH STATUS 1\n"
DAG_OK = "10/25/26 10:00:00 **** condor_dagman (condor_DAGMAN) pid 4242 EXITING WITH STATUS 0\n"


class T(unittest.TestCase):
    def test_summarize(self):
        s = cli.summarize("j", 6, 2, [("j", 2, "slot1_1@worker1"), ("j", 1, "undefined")])
        for x in ("j", "2/6 done", "1 running", "worker1", "1 idle"):
            self.assertIn(x, s)

    def test_shard_rows_excludes_non_shard(self):
        rows = cli.shard_rows([["j", "2", "undefined", "undefined"], ["j", "2", "slot1@w1", "j"]])
        self.assertEqual(rows, [("j", 2, "slot1@w1")])
        self.assertNotIn("undefined", cli.summarize("j", 1, 0, rows))

    def merge(self, **files):
        d = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name, text in files.items():
            (d / name.replace("_", ".")).write_text(text)
        return cli.merge_state(d)

    def test_merge_state(self):
        self.assertEqual(self.merge(), "pending")
        self.assertEqual(self.merge(merge_log=OK), "done")
        self.assertEqual(self.merge(merge_log=BAD), "failed")
        self.assertEqual(self.merge(job_dag_dagman_out=DAG_FAIL), "failed")  # shards failed, merge never ran
        self.assertEqual(self.merge(job_dag_rescue001=""), "failed")
        self.assertEqual(self.merge(job_dag_dagman_out="running\n"), "pending")
        self.assertEqual(self.merge(merge_log=OK, job_dag_dagman_out=DAG_OK), "done")

    def test_cancel_rejects_bad_name(self):
        with mock.patch("swarm.cli.subprocess.run") as run, redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["cancel", 'x" || true || "']), 2)
            run.assert_not_called()

    def test_status_skips_prev(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "j").mkdir()
        (root / "j.prev").mkdir()
        out = io.StringIO()
        with mock.patch.object(cli, "ROOT", root), mock.patch.object(cli, "_q", return_value=[]), \
                mock.patch("sys.stdout", out):
            cli.status()
        self.assertNotIn("j.prev", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class Wait(unittest.TestCase):
    def run_wait(self, states, timeout=None):
        argv = ["wait", "j"] + (["--timeout", str(timeout)] if timeout is not None else [])
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        (Path(d.name) / "j").mkdir()
        with mock.patch.object(cli, "ROOT", Path(d.name)), mock.patch.object(cli, "merge_state", side_effect=states), \
             mock.patch.object(cli.time, "sleep"), mock.patch.object(cli.time, "monotonic", side_effect=range(0, 10000, 30)):
            return cli.main(argv)

    def test_done(self):
        self.assertEqual(self.run_wait(["pending", "pending", "done"]), 0)

    def test_failed(self):
        self.assertEqual(self.run_wait(["pending", "failed"]), 1)

    def test_timeout(self):
        self.assertEqual(self.run_wait(["pending"] * 100, timeout=60), 124)

    def test_bad_name(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["wait", "../x"]), 2)


class Update(unittest.TestCase):
    def test_execs_install_update(self):
        with mock.patch.object(cli.os, "execvp") as ex:
            cli.main(["update"])
        ex.assert_called_once_with("sudo", ["sudo", cli._install_sh(), "update"])


class WaitMissing(unittest.TestCase):
    def test_missing_job(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(cli, "ROOT", Path(d)), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["wait", "nope"]), 2)

    def test_update_as_root_skips_sudo(self):
        with mock.patch.object(cli.os, "geteuid", return_value=0), mock.patch.object(cli.os, "execvp") as ex:
            cli.main(["update"])
        ex.assert_called_once_with(cli._install_sh(), [cli._install_sh(), "update"])
