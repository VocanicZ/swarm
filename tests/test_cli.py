import unittest

from swarm import cli


class T(unittest.TestCase):
    def test_summarize(self):
        s = cli.summarize("j", 6, 2, [("j", 2, "slot1_1@NathanielSong"), ("j", 1, "undefined")])
        for x in ("j", "2/6 done", "1 running", "NathanielSong", "1 idle"):
            self.assertIn(x, s)


if __name__ == "__main__":
    unittest.main()
