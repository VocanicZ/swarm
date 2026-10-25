import contextlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ["SWARM_POLL_SECONDS"] = "0.2"
from swarm import shard  # noqa: E402

VERSION = "0123456789abcdef"
ROOT = Path(__file__).resolve().parent.parent


class ShardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()
        (self.run_dir / "gpu0.lock").touch(mode=0o666)
        self.enterContext(mock.patch.dict(os.environ, {"SWARM_RUN_DIR": str(self.run_dir)}))
        (self.tmp / "markers").mkdir()  # stands in for /var/tmp, which always exists
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
                "gpu": "no", "vram_mb": 0, "save_seconds": 60, "version": VERSION}
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

    def test_gpu_stale_file_falls_back(self):
        # governor dead: a leftover ok=true gpu.json must not grant the GPU
        self.set_gpu(True)
        old = time.time() - 30
        os.utime(self.run_dir / "gpu.json", (old, old))
        box = self.sandbox('echo "$SWARM_GPU" > "$2/mode"\n', gpu="optional", vram_mb=1000)
        self.assertEqual(shard.main(["s1"]), 0)
        self.assertEqual((box / "out/mode").read_text(), "0\n")

    def test_gpu_poll_fast(self):
        with mock.patch.object(shard, "POLL", 5.0):
            self.assertEqual(shard.poll_s(True), 1.0)
            self.assertEqual(shard.poll_s(False), 5.0)

    def test_gpu_required_waits(self):
        box = self.sandbox('echo "$SWARM_GPU $CUDA_VISIBLE_DEVICES" > "$2/mode"\n',
                           gpu="required", vram_mb=1000)
        threading.Timer(0.5, self.set_gpu, [True]).start()
        t = time.monotonic()
        self.assertEqual(shard.main(["s1"]), 0)
        self.assertGreaterEqual(time.monotonic() - t, 0.5)
        self.assertEqual((box / "out/mode").read_text(), "1 0\n")

    def test_gpu_revoked_restarts_on_cpu(self):
        # The GPU run ignores SIGTERM: revoke must SIGKILL at once, not wait KILL_GRACE.
        self.set_gpu(True)
        box = self.sandbox('[ "$SWARM_GPU" = 1 ] && trap "" TERM\n'
                           'echo "$SWARM_GPU" >> "$2/mode"\nsleep 30\n',
                           gpu="optional", vram_mb=1000, save_seconds=2)
        threading.Timer(0.5, self.set_gpu, [False]).start()
        t = time.monotonic()
        self.assertEqual(shard.main(["s1"]), 85)
        self.assertLess(time.monotonic() - t, 5)  # 3 s hard limit + slack, far under KILL_GRACE
        self.assertEqual((box / "out/mode").read_text(), "1\n0\n")

    def setup_job(self, setup="touch $HOME_OK; echo x >> $COUNT"):
        self.ok, self.count = self.tmp / "ok", self.tmp / "count"
        self.enterContext(mock.patch.dict(
            os.environ, {"HOME_OK": str(self.ok), "COUNT": str(self.count)}))
        return dict(check="test -f $HOME_OK", setup=setup)

    def test_unsafe_marker_dir_ignored(self):
        d = shard.MARKER_ROOT / f"swarm-setup-{os.getuid()}"
        d.mkdir(parents=True)
        d.chmod(0o777)
        (d / VERSION).touch()  # planted marker must not skip setup
        self.sandbox("exit 0\n", **self.setup_job())
        self.assertEqual(shard.main(["s1"]), 0)
        self.assertEqual(self.count.read_text().splitlines(), ["x"])

    def test_bad_version_no_marker(self):
        self.sandbox("exit 0\n", version="../evil", **self.setup_job())
        self.assertEqual(shard.main(["s1"]), 0)
        self.assertEqual(self.count.read_text().splitlines(), ["x"])
        made = [p for p in self.tmp.rglob("*") if p.is_file() and p.name != ".lock"
                and shard.MARKER_ROOT in p.parents]
        self.assertEqual(made, [])
        self.assertFalse((self.tmp / "evil").exists())

    def test_setup_once_concurrent(self):
        job = self.setup_job("sleep 0.5; echo x >> $COUNT; touch $HOME_OK")
        code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); from pathlib import Path; "
                f"from swarm import shard; shard.MARKER_ROOT = Path({str(shard.MARKER_ROOT)!r}); "
                "sys.exit(shard.main(['s1']))")
        procs = []
        for _ in range(2):
            box = self.sandbox("exit 0\n", **job)
            procs.append(subprocess.Popen([sys.executable, "-c", code], cwd=box))
        self.assertEqual([p.wait(30) for p in procs], [0, 0])
        self.assertEqual(self.count.read_text().splitlines(), ["x"])

if __name__ == "__main__":
    unittest.main()
