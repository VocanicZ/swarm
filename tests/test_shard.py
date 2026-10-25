import contextlib
import json
import os
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ["SWARM_POLL_SECONDS"] = "0.2"
from swarm import shard  # noqa: E402


class ShardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()
        (self.run_dir / "gpu0.lock").touch(mode=0o666)
        self.enterContext(mock.patch.dict(os.environ, {"SWARM_RUN_DIR": str(self.run_dir)}))
        self.enterContext(mock.patch.object(shard, "MARKER_ROOT", self.tmp / "markers"))
        self.n = 0

    def sandbox(self, script, **job):
        """Fresh sandbox dir (cwd) with workdir.tar.gz holding run.sh, plus job.json."""
        self.n += 1
        box = self.tmp / f"box{self.n}"
        src = self.tmp / f"src{self.n}"
        src.mkdir()
        box.mkdir()
        (src / "run.sh").write_text(script)
        with tarfile.open(box / "workdir.tar.gz", "w:gz") as t:
            t.add(src, arcname=".")
        spec = {"run": "sh run.sh {shard} {out}", "check": None, "setup": None,
                "gpu": "no", "vram_mb": 0, "save_seconds": 60, "version": "v1"}
        spec.update(job)
        (box / "job.json").write_text(json.dumps(spec))
        self.enterContext(contextlib.chdir(box))
        return box

    def set_gpu(self, ok, free=4000):
        p = self.run_dir / "gpu.json"
        p.with_suffix(".tmp").write_text(json.dumps({"ok": ok, "free_vram_mb": free}))
        p.with_suffix(".tmp").replace(p)

    def test_done_exit0(self):
        box = self.sandbox('echo hi > "$2/r"\n')
        self.assertEqual(shard.main(["s1"]), 0)
        self.assertTrue((box / "out/r").exists())

    def test_run_failure_code(self):
        self.sandbox("exit 3\n")
        self.assertEqual(shard.main(["s1"]), 3)

    def test_checkpoint_after_write(self):
        box = self.sandbox('while :; do date > "$2/p"; sleep 0.3; done\n', save_seconds=1)
        t = time.monotonic()
        self.assertEqual(shard.main(["s1"]), 85)
        self.assertLess(time.monotonic() - t, 3)
        self.assertTrue((box / "out/p").exists())

    def test_checkpoint_sigterm_ignored(self):
        self.sandbox("trap '' TERM\nwhile :; do date > \"$2/p\"; sleep 0.3; done\n", save_seconds=1)
        t = time.monotonic()
        self.assertEqual(shard.main(["s1"]), 85)
        self.assertGreaterEqual(time.monotonic() - t, 1 + shard.KILL_GRACE)  # grace honoured
        self.assertLess(time.monotonic() - t, 1 + 1.5 + shard.KILL_GRACE + 2)

    def test_checkpoint_without_writes(self):
        self.sandbox("sleep 30\n", save_seconds=2)
        t = time.monotonic()
        self.assertEqual(shard.main(["s1"]), 85)
        self.assertGreaterEqual(time.monotonic() - t, 2.5)
        self.assertLess(time.monotonic() - t, 4)

    def test_resume_skips_extract(self):
        box = self.sandbox('echo hi > "$2/r"\n')
        self.assertEqual(shard.main(["s1"]), 0)
        (box / "work/run.sh").write_text("exit 7\n")
        self.assertEqual(shard.main(["s1"]), 7)

    def test_unfit(self):
        box = self.sandbox('touch "$2/ran"\n', check="false", setup="true")
        self.assertEqual(shard.main(["s1"]), 86)
        self.assertFalse((box / "out/ran").exists())

    def test_setup_once(self):
        ok, count = self.tmp / "ok", self.tmp / "count"
        self.enterContext(mock.patch.dict(os.environ, {"HOME_OK": str(ok), "COUNT": str(count)}))
        job = dict(check="test -f $HOME_OK", setup="touch $HOME_OK; echo x >> $COUNT")
        self.sandbox("exit 0\n", **job)
        self.assertEqual(shard.main(["s1"]), 0)
        ok.unlink()
        self.sandbox("exit 0\n", **job)
        self.assertEqual(shard.main(["s1"]), 86)
        self.assertEqual(count.read_text().splitlines(), ["x"])

    def test_gpu_optional_falls_back(self):
        box = self.sandbox('echo "$SWARM_GPU" > "$2/mode"\n', gpu="optional")
        self.assertEqual(shard.main(["s1"]), 0)
        self.assertEqual((box / "out/mode").read_text(), "0\n")

    def test_gpu_required_waits(self):
        box = self.sandbox('echo "$SWARM_GPU $CUDA_VISIBLE_DEVICES" > "$2/mode"\n',
                           gpu="required", vram_mb=1000)
        threading.Timer(0.5, self.set_gpu, [True]).start()
        t = time.monotonic()
        self.assertEqual(shard.main(["s1"]), 0)
        self.assertGreaterEqual(time.monotonic() - t, 0.5)
        self.assertEqual((box / "out/mode").read_text(), "1 0\n")

    def test_gpu_revoked_restarts_on_cpu(self):
        self.set_gpu(True)
        box = self.sandbox('echo "$SWARM_GPU" >> "$2/mode"\nsleep 30\n',
                           gpu="optional", vram_mb=1000, save_seconds=2)
        threading.Timer(0.5, self.set_gpu, [False]).start()
        self.assertEqual(shard.main(["s1"]), 85)
        self.assertEqual((box / "out/mode").read_text(), "1\n0\n")


if __name__ == "__main__":
    unittest.main()
