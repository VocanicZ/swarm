import tempfile
import unittest
from pathlib import Path

from swarm.jobspec import SpecError, load

BASE = 'name = "j"\nsplit = "s"\nrun = "r {out}"\nmerge = "m {outs}"\n'


def spec(extra="", base=BASE):
    d = tempfile.TemporaryDirectory()
    p = Path(d.name)
    (p / "swarm.toml").write_text(base + extra)
    return d, p


class JobSpecTest(unittest.TestCase):
    def check_err(self, extra="", base=BASE, msg=None):
        d, p = spec(extra, base)
        with d, self.assertRaises(SpecError) as cm:
            load(p)
        if msg:
            self.assertIn(msg, str(cm.exception))

    def test_minimal_defaults(self):
        d, p = spec()
        with d:
            s = load(p)
            self.assertEqual(s.workdir, p)
            self.assertEqual((s.gpu, s.vram_mb, s.threads, s.mem_mb, s.save_seconds),
                             ("no", 0, 1, 1024, 3600))
            self.assertIsNone(s.setup)
            self.assertIsNone(s.check)

    def test_unknown_key_rejected(self):
        self.check_err("thread = 4\n", msg="thread")

    def test_bad_gpu_value(self):
        self.check_err('gpu = "maybe"\n')

    def test_gpu_needs_vram(self):
        self.check_err('gpu = "required"\n')

    def test_run_needs_placeholders(self):
        self.check_err(base=BASE.replace("{out}", "x"))
        self.check_err(base=BASE.replace("{outs}", "x"))

    def test_bad_name(self):
        self.check_err(base=BASE.replace('"j"', '"a b"'))
        self.check_err(base=BASE.replace('"j"', '"x\\n"'))  # `$` would match before a trailing newline
        self.check_err(base=BASE.replace('"j"', '"x.prev"'))  # reserved for a resubmitted job's old state

    def test_missing_workdir(self):
        self.check_err('workdir = "nope"\n')

    def test_int_bounds(self):
        self.check_err("threads = 0\n")


if __name__ == "__main__":
    unittest.main()
