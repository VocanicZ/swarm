import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from swarm import tick
from swarm.tick import coord_free_cores, dead_machines

NOW = 10_000


class TickTests(unittest.TestCase):
    def test_dead_machines(self):
        d = lambda rows: dead_machines(rows, NOW, "COORD")
        self.assertEqual(d([("A", NOW - 200)]), ["A"])
        self.assertEqual(d([("A", NOW - 100)]), [])
        self.assertEqual(d([("A", NOW - 200), ("A", NOW - 10)]), [])
        self.assertEqual(d([("COORD", NOW - 999)]), [])
        self.assertEqual(d([("B", NOW - 300), ("A", NOW - 300), ("A", NOW - 400), ("C", None), ("D", "x")]), ["A", "B"])

    def test_coord_free_cores(self):
        self.assertEqual(coord_free_cores(32, 20e6 * 60, 4e6 * 60, 60), 16.0)
        self.assertEqual(coord_free_cores(32, 40e6 * 60, 0, 60), 0)
        self.assertEqual(coord_free_cores(32, 0, 10e6 * 60, 60), 32)


class TickNeverCollects(unittest.TestCase):
    """The root coordinator tick must not delete job state: GC belongs to the submitting user."""

    def test_tick_does_not_gc_or_delete_job_state(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / "coordinator.conf").write_text("CPU_LIMIT=4\nCOORD_MACHINE=coord\n")
        (tmp / "cg").mkdir()
        for f in (tmp / "root.stat", tmp / "cg/cpu.stat"):
            f.write_text("usage_usec 1000\n")
        old = tmp / "home/.swarm/jobs/old"  # terminal, long past retention: GC would remove it
        (old / "shards/a/out").mkdir(parents=True)
        (old / "job.json").write_text("{}")
        (old / "merge.log").write_text("005 (1.000.000) Job terminated.\n\t(1) Normal termination (return value 0)\n")
        t = time.time() - 48 * 3600
        for p in (old / "shards/a/out", old / "shards/a", old / "shards", old / "job.json", old / "merge.log", old):
            os.utime(p, (t, t))
        status = subprocess.CompletedProcess([], 0, stdout=f"dead {int(t)}\ncoord {int(t)}\n", stderr="")
        with mock.patch.object(tick, "CONF", tmp / "coordinator.conf"), \
                mock.patch.object(tick, "STATE", tmp / "tick.json"), \
                mock.patch.object(tick, "ROOT_STAT", tmp / "root.stat"), \
                mock.patch.object(tick, "CG", tmp / "cg"), \
                mock.patch.dict(os.environ, {"HOME": str(tmp / "home")}), \
                mock.patch("swarm.tick.subprocess.run", return_value=status) as run, \
                mock.patch("swarm.gc.collect") as collect, \
                mock.patch("shutil.rmtree") as rmtree:
            for _ in range(2):  # second tick has a "last" sample, exercising the qedit path
                self.assertEqual(tick.main(), 0)
        collect.assert_not_called()
        rmtree.assert_not_called()
        self.assertTrue(old.exists())
        self.assertNotIn("condor_q", [c.args[0][0] for c in run.call_args_list])
        self.assertFalse(hasattr(tick, "gc"))


if __name__ == "__main__":
    unittest.main()
