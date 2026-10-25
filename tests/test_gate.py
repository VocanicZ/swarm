import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "condor/worker.config"


def gate():
    for line in CONFIG.read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip() == "SWARM_GATE":
            return v.strip()
    raise AssertionError("no SWARM_GATE in worker.config")


def start_expr():
    # START as condor itself expands it ($(SWARM_GATE) is textual macro substitution)
    with tempfile.TemporaryDirectory() as d:
        cfg = CONFIG.read_text().replace("@IP@", "127.0.0.1").replace("@SLOT_USERS@", "")
        Path(d, "worker.config").write_text(cfg)
        Path(d, "condor_config").write_text(
            f"LOCAL_DIR = {d}\nLOCAL_CONFIG_FILE =\nLOCAL_CONFIG_DIR =\ninclude : {d}/worker.config\n")
        r = subprocess.run(["condor_config_val", "START"], capture_output=True, text=True,
                           env={"CONDOR_CONFIG": f"{d}/condor_config", "PATH": "/usr/bin:/bin"})
    if r.returncode:
        raise AssertionError(r.stderr or r.stdout)
    return r.stdout.strip()


def evaluate(my: str, target: str, expr=None) -> str:
    # classad_eval only parses multi-attribute files in new-ClassAd [ a = 1; b = 2 ] form
    with tempfile.TemporaryDirectory() as d:
        m, t = Path(d, "my.ad"), Path(d, "target.ad")
        m.write_text(f"[ {my} ]\n")
        t.write_text(f"[ {target} ]\n")
        r = subprocess.run(["classad_eval", "-my-file", str(m), "-target-file", str(t), "-quiet", expr or gate()],
                           capture_output=True, text=True)
    if r.returncode:
        raise AssertionError(r.stderr or r.stdout)
    return r.stdout.strip()


@unittest.skipUnless(shutil.which("classad_eval"), "HTCondor classad_eval not installed")
class GateTest(unittest.TestCase):
    TARGET = "SwarmInputMB = 50; SwarmCoordRate = 16"

    def test_gate_adds_20pct(self):
        self.assertEqual(evaluate("SwarmCapCores = 12; SwarmLinkMbps = 100", self.TARGET), "true")

    def test_gate_too_small(self):
        # 2 * (60 - 50*8/100/60 - 1) = 117.9 < 0.2 * 16 * 60 = 192
        self.assertEqual(evaluate("SwarmCapCores = 2; SwarmLinkMbps = 100", self.TARGET), "false")

    def test_gate_no_coord_rate(self):
        self.assertEqual(evaluate("SwarmCapCores = 2; SwarmLinkMbps = 100", "SwarmInputMB = 50"), "true")

    @unittest.skipUnless(shutil.which("condor_config_val"), "condor_config_val not installed")
    def test_start_respects_cap_when_gate_passes(self):
        # gate passes (coord rate 0) but 8 cores in use + 4 requested > cap 2 -> must not start
        my = "SwarmCapCores = 2; SwarmLinkMbps = 100; TotalSlotCpus = 8; Cpus = 0"
        target = "RequestCpus = 4; SwarmInputMB = 50; SwarmCoordRate = 0"
        self.assertEqual(evaluate(my, target, start_expr()), "false")


if __name__ == "__main__":
    unittest.main()
