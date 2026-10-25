import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swarm import governor as g

STAT = "cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 50 0 50 350 50 0 0 0 0 0\n"


class GovernorTests(unittest.TestCase):
    def test_cap_pct(self):
        self.assertEqual(g.cap_pct(20), 70)
        self.assertEqual(g.cap_pct(30), 60)
        self.assertEqual(g.cap_pct(0), 90)
        self.assertEqual(g.cap_pct(95), 0)

    def test_smoother(self):
        s = g.Smoother()
        self.assertEqual(s.push(0, 70), 70)
        self.assertEqual(s.push(1, 20), 20)
        self.assertEqual(s.push(20, 70), 20)
        self.assertEqual(s.push(31.5, 70), 70)

    def test_cpu_times(self):
        # total 1000, idle 700 + iowait 100
        self.assertEqual(g.cpu_times(STAT), (200, 1000))

    def test_parse_nvidia(self):
        r = g.parse_nvidia("14, 8192, 1736", "1234, 500\n5678, 300")
        self.assertEqual(r, {"util_pct": 14, "total_mb": 8192,
                             "used_mb": 1736, "procs": {1234: 500, 5678: 300}})
        self.assertEqual(g.parse_nvidia("0, 8192, 0", "")["procs"], {})

    def test_gpu_state(self):
        self.assertEqual(g.gpu_state(50, 8192, 1000), {"ok": True, "free_vram_mb": 6372})
        self.assertFalse(g.gpu_state(95, 8192, 0)["ok"])
        self.assertEqual(g.gpu_state(0, 8192, 8000), {"ok": False, "free_vram_mb": 0})

    def test_startd_ad(self):
        ad = g.startd_ad(60, 20, 100, 1, 6372)
        self.assertIn("SwarmCapCores = 12", ad)
        self.assertIn("SwarmGpuFreeMB = 6372", ad)
        self.assertIn("SwarmLinkMbps = 100", ad)
        self.assertIn("SwarmGpus = 1", ad)

    def test_no_nvidia(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(g.read_gpu())
        self.assertEqual(g.publish_gpu(None, 0, 0), {"ok": False, "free_vram_mb": 0})

    def test_parse_pmon(self):
        txt = ("# gpu         pid   type     sm    mem    enc    dec    jpg    ofa    command\n"
               "# Idx           #    C/G      %      %      %      %      %      %    name\n"
               "    0       1234     C     45     10      -      -      -      -    python3\n"
               "    0       5678     G      -      -      -      -      -      -    Xorg\n")
        self.assertEqual(g.parse_pmon(txt), {1234: 45, 5678: 0})

    def test_mem_step(self):
        st = (None, None)
        st, a = g.mem_step(st, 0, 10, 5)
        self.assertIsNone(a)
        st, a = g.mem_step(st, 30, 10, 5)
        self.assertIsNone(a)  # not yet >30 s
        st, a = g.mem_step(st, 31, 10, 5)
        self.assertEqual(a, "freeze")
        st, a = g.mem_step(st, 50, 10, 5)
        self.assertIsNone(a)
        st, a = g.mem_step(st, 62, 10, 5)
        self.assertEqual(a, "vacate")
        self.assertEqual(st, (None, None))
        # back under while frozen -> unfreeze
        st, _ = g.mem_step((None, None), 0, 10, 5)
        st, a = g.mem_step(st, 31, 10, 5)
        st, a = g.mem_step(st, 32, 4, 5)
        self.assertEqual(a, "unfreeze")
        self.assertEqual(st, (None, None))

    def test_cg_missing(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(g, "CG", Path(d) / "nope"):
            self.assertEqual(g.measure_cg(), (0, 0, 0, False))
            g._write("cpu.max", "1 1")  # must be a silent no-op

    def test_cg_present(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(g, "CG", Path(d)):
            (Path(d) / "cpu.stat").write_text("usage_usec 500\nuser_usec 1\n")
            (Path(d) / "memory.current").write_text("4096\n")
            (Path(d) / "memory.stat").write_text("anon 1024\nfile 3072\n")
            self.assertEqual(g.measure_cg(), (500, 4096, 1024, True))

    def test_owner_gpu(self):
        gpu = {"util_pct": 70, "used_mb": 1000, "procs": {11: 400, 22: 100}, "sm": {11: 45, 22: 5}}
        with mock.patch.object(g, "_swarm_pids", return_value={11}):
            self.assertEqual(g.owner_gpu(gpu), (600, 25))
            gpu["sm"] = None
            self.assertEqual(g.owner_gpu(gpu), (600, 0))


if __name__ == "__main__":
    unittest.main()
