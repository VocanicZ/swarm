import unittest

from swarm import cli


class T(unittest.TestCase):
    def test_summarize(self):
        s = cli.summarize("j", 6, 2, [("j", 2, "slot1_1@NathanielSong"), ("j", 1, "undefined")])
        for x in ("j", "2/6 done", "1 running", "NathanielSong", "1 idle"):
            self.assertIn(x, s)

    def test_shard_rows_excludes_non_shard(self):
        rows = cli.shard_rows([["j", "2", "undefined", "undefined"], ["j", "2", "slot1@w1", "j"]])
        self.assertEqual(rows, [("j", 2, "slot1@w1")])
        self.assertNotIn("undefined", cli.summarize("j", 1, 0, rows))


if __name__ == "__main__":
    unittest.main()
