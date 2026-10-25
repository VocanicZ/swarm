import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swarm import jobspec, submit

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
        self.assertIn(f"cd {spec.jobdir}", merge)
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


if __name__ == "__main__":
    unittest.main()
