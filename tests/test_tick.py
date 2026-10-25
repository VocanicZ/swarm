import unittest

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


if __name__ == "__main__":
    unittest.main()
