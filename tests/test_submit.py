import io
import os
import subprocess
import tarfile
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from swarm import gc, jobspec, submit

REAL_RUN = subprocess.run
TOML = '''name = "toy"
split = "printf 'a\\nb\\nc\\n'"
run = "echo hi > {out}/x"
merge = "cat {outs}"
'''


def mkjob(extra="", toml=TOML):
    d = Path(tempfile.mkdtemp())
    (d / "swarm.toml").write_text(toml + extra)
    (d / "data.txt").write_text("hello")
    return d


class T(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"SWARM_SHARD_PY": "/x/shard.py"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.state = Path(tempfile.mkdtemp()) / "st"

    def test_build_files(self):
        spec = jobspec.load(mkjob("threads = 2\nmem_mb = 2048\n"))
        dag = submit.build(spec, ["a", "b", "c"], self.state)
        self.assertEqual(dag, self.state / "job.dag")
        self.assertEqual(dag.read_text().splitlines(),
                         ["JOB shards shards.sub", "JOB merge merge.sub", "PARENT shards CHILD merge"])
        sub = (self.state / "shards.sub").read_text().splitlines()
        s = str(self.state)
        for line in [
            "executable                = /x/shard.py",
            "arguments                 = $(shard)",
            "initialdir                = shards/$(shard)",
            f"transfer_input_files      = {s}/workdir.tar.gz, {s}/job.json",
            "should_transfer_files     = yes",
            "checkpoint_exit_code      = 85",
            "transfer_checkpoint_files = out",
            "transfer_output_files     = out",
            "job_lease_duration        = 180",
            "max_retries               = 3",
            "job_machine_attrs         = Machine",
            "requirements              = (MY.ExitCode =!= 86 || TARGET.Machine =!= MY.MachineAttrMachine0)",
            "rank                      = 0",
            "request_cpus              = 2",
            "request_memory            = 2048",
            "request_disk              = 1026M",
            '+SwarmJob                 = "toy"',
            '+SwarmGpu                 = "no"',
            "output = run.out",
            "error  = run.err",
            f"log    = {s}/shards.log",
            "queue shard from shards.txt",
        ]:
            self.assertIn(line, sub)
        self.assertTrue(any(l.startswith("+SwarmInputMB             = 0.") for l in sub))
        merge = (self.state / "merge.sh").read_text()
        outs = " ".join(f"{s}/shards/{i}/out" for i in "abc")
        self.assertIn(outs, merge)
        self.assertIn(f"cd {spec.jobdir} && cat ", merge)
        self.assertNotIn("exec", merge)
        for i in "abc":
            self.assertTrue((self.state / "shards" / i).is_dir())
        self.assertEqual((self.state / "shards.txt").read_text().split(), ["a", "b", "c"])
        self.assertIn("universe", (self.state / "merge.sub").read_text())
        import json
        j = json.loads((self.state / "job.json").read_text())
        self.assertEqual(len(j["version"]), 16)

    def test_gpu_required_lines(self):
        spec = jobspec.load(mkjob('gpu = "required"\nvram_mb = 2000\n'))
        submit.build(spec, ["a"], self.state)
        req = [l for l in (self.state / "shards.sub").read_text().splitlines() if l.startswith("requirements")][0]
        self.assertTrue(req.endswith("&& TARGET.SwarmGpus >= 1 && TARGET.SwarmGpuFreeMB >= 2000"))

    def test_gpu_optional_rank(self):
        spec = jobspec.load(mkjob('gpu = "optional"\nvram_mb = 2000\n'))
        submit.build(spec, ["a"], self.state)
        self.assertIn("rank                      = ifThenElse(TARGET.SwarmGpuFreeMB >= 2000, 1, 0)",
                      (self.state / "shards.sub").read_text())

    def test_swarmignore(self):
        d = mkjob()
        (d / ".swarmignore").write_text("data.txt\n")
        (d / "keep.txt").write_text("k")
        submit.build(jobspec.load(d), ["a"], self.state)
        with tarfile.open(self.state / "workdir.tar.gz") as t:
            names = [n.lstrip("./") for n in t.getnames()]
        self.assertNotIn("data.txt", names)
        self.assertIn("keep.txt", names)

    def test_split_rejects(self):
        for cmd, bad in [("printf 'a b\\n'", "a b"), ("echo ../x", "../x"),
                         ("printf 'a\\na\\n'", "a"), ("true", None)]:
            with self.subTest(cmd=cmd):
                spec = jobspec.load(mkjob(toml=TOML.replace(TOML.splitlines()[1], f'split = "{cmd}"')))
                with self.assertRaises(jobspec.SpecError) as c:
                    submit.split(spec)
                if bad:
                    self.assertIn(bad, str(c.exception))

    def test_split_ok(self):
        self.assertEqual(submit.split(jobspec.load(mkjob())), ["a", "b", "c"])

    def test_resubmit_refused(self):
        d = mkjob()
        r = subprocess.CompletedProcess([], 0, stdout="toy\n", stderr="")
        with mock.patch("swarm.submit.subprocess.run", return_value=r) as run, \
                mock.patch("swarm.submit.build") as b:
            with self.assertRaises(jobspec.SpecError):
                submit.submit(d, Path(tempfile.mkdtemp()))
            b.assert_not_called()

    def test_condor_q_failure_refuses(self):
        # schedd down: empty stdout must not look like "not queued" -> no build (which moves the state dir)
        d = mkjob()
        for effect in (subprocess.CompletedProcess([], 1, stdout="", stderr="cannot reach schedd"),
                       FileNotFoundError("condor_q"), subprocess.TimeoutExpired("condor_q", 60)):
            with self.subTest(effect=effect), mock.patch("swarm.submit.split", return_value=["a"]), \
                    mock.patch("swarm.submit.subprocess.run",
                               **({"return_value": effect} if isinstance(effect, subprocess.CompletedProcess)
                                  else {"side_effect": effect})), \
                    mock.patch("swarm.submit.build") as b:
                with self.assertRaises(jobspec.SpecError) as c:
                    submit.submit(d, Path(tempfile.mkdtemp()))
                self.assertIn("condor_q", str(c.exception))
                b.assert_not_called()

    def test_rebuild_keeps_previous_state(self):
        spec = jobspec.load(mkjob())
        prev = self.state.with_name("st.prev")
        submit.build(spec, ["a"], self.state)
        (self.state / "shards/a/out").mkdir()
        (self.state / "shards/a/out/x").write_text("1")
        submit.build(spec, ["a"], self.state)
        self.assertEqual((prev / "shards/a/out/x").read_text(), "1")
        self.assertFalse((self.state / "shards/a/out").exists())
        (self.state / "marker").write_text("2")
        submit.build(spec, ["a"], self.state)  # an older .prev is replaced
        self.assertTrue((prev / "marker").exists())
        self.assertFalse((prev / "shards/a/out").exists())


OK_LOG = "005 (12.000.000) 2026-10-25 10:00:00 Job terminated.\n\t(1) Normal termination (return value 0)\n"


def old_job(root, name, hours=48):
    d = Path(root) / name
    (d / "shards/a/out").mkdir(parents=True)
    (d / "job.json").write_text("{}")
    (d / "merge.log").write_text(OK_LOG)
    t = time.time() - hours * 3600
    for top, dirs, files in os.walk(d):
        for n in dirs + files:
            os.utime(os.path.join(top, n), (t, t))
    os.utime(d, (t, t))
    return d


class SubmitGc(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {"SWARM_SHARD_PY": "/x/shard.py"}))
        os.environ.pop("SWARM_JOB_RETENTION_HOURS", None)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "jobs"
        self.root.mkdir()
        self.run = self.enterContext(mock.patch(
            "swarm.submit.subprocess.run", side_effect=self.fake_run))
        self.calls = []

    def fake_run(self, cmd, *a, **kw):
        self.calls.append(cmd[0])
        if cmd[0] == "condor_q":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "condor_submit_dag":
            return subprocess.CompletedProcess(cmd, 0)
        return REAL_RUN(cmd, *a, **kw)  # split's sh, build's tar

    def test_submit_gcs_its_state_root_before_build(self):
        order = []
        with mock.patch("swarm.submit.gc.collect", side_effect=lambda *a, **k: order.append("gc") or []) as c, \
                mock.patch("swarm.submit.build", side_effect=lambda *a: order.append("build")):
            self.assertEqual(submit.submit(mkjob(), self.root), "toy")
        c.assert_called_once()
        self.assertEqual(Path(c.call_args.args[0]), self.root)
        self.assertEqual(c.call_args.args[1], gc.DEFAULT_RETENTION_HOURS)
        self.assertFalse(c.call_args.kwargs.get("dry_run", False))
        self.assertIn("toy", c.call_args.kwargs["queued"])
        self.assertEqual(order, ["gc", "build"])

    def test_gc_failure_does_not_fail_submit(self):
        for exc in (jobspec.SpecError("condor_q failed"), PermissionError("denied"),
                    OSError("rmtree"), RuntimeError("boom")):
            with self.subTest(exc=exc), mock.patch("swarm.submit.gc.collect", side_effect=exc), \
                    redirect_stderr(io.StringIO()) as err:
                self.calls.clear()
                self.assertEqual(submit.submit(mkjob(), self.root), "toy")
                self.assertIn("condor_submit_dag", self.calls)
                self.assertIn(str(exc), err.getvalue())
                self.assertTrue((self.root / "toy/job.dag").exists())

    def test_gc_condor_q_failure_does_not_fail_submit(self):
        old = old_job(self.root, "old")
        down = [subprocess.CompletedProcess([], 1, stdout="", stderr="schedd down")]

        def run(cmd, *a, **kw):  # gc's condor_q (first) fails, submit's own queued check succeeds
            if cmd[0] == "condor_q" and down:
                return down.pop()
            return self.fake_run(cmd, *a, **kw)
        self.run.side_effect = run
        with redirect_stderr(io.StringIO()):
            self.assertEqual(submit.submit(mkjob(), self.root), "toy")
        self.assertTrue(old.exists())

    def test_submit_collects_old_terminal_job(self):
        old = old_job(self.root, "old")
        fresh = old_job(self.root, "fresh", hours=1)
        self.assertEqual(submit.submit(mkjob(), self.root), "toy")
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue((self.root / "toy/job.dag").exists())
        self.assertIn("condor_submit_dag", self.calls)

    def test_resubmit_keeps_own_old_run_as_prev(self):
        own = old_job(self.root, "toy")
        (own / "shards/a/out/x").write_text("1")
        os.utime(own / "shards/a/out/x", (time.time() - 48 * 3600,) * 2)
        self.assertEqual(submit.submit(mkjob(), self.root), "toy")
        self.assertEqual((self.root / "toy.prev/shards/a/out/x").read_text(), "1")
        self.assertTrue((self.root / "toy/job.dag").exists())



if __name__ == "__main__":
    unittest.main()
