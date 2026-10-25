import unittest

from swarm.tick import coord_free_cores, parse_addr, update_misses

ADDR = "<192.168.1.123:9618?addrs=192.168.1.123-9618+[fd7a-115c-a1e0--6b34-b26e]-9618&alias=NathanielSong&noUDP&sock=startd_1_2>"


class TickTests(unittest.TestCase):
    def test_parse_addr(self):
        self.assertEqual(parse_addr(ADDR), ("192.168.1.123", 9618))

    def test_update_misses(self):
        m, dead = {}, None
        out = []
        for _ in range(3):
            m, dead = update_misses(m, {"A": False, "B": True})
            out.append(dead)
        self.assertEqual(out, [[], [], ["A"]])
        self.assertEqual(m, {"A": 0, "B": 0})
        m, _ = update_misses({}, {"A": False})
        m, _ = update_misses(m, {"A": True})
        self.assertEqual(m["A"], 0)

    def test_coord_free_cores(self):
        self.assertEqual(coord_free_cores(32, 20e6 * 60, 4e6 * 60, 60), 16.0)
        self.assertEqual(coord_free_cores(32, 40e6 * 60, 0, 60), 0)


if __name__ == "__main__":
    unittest.main()
