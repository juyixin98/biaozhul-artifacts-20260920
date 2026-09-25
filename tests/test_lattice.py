import unittest

from lattlang.analysis import (
    BOTTOM,
    TOP,
    LatticeValue,
    lattice_meet,
)


class TestLattice(unittest.TestCase):
    def test_meet_identity_top(self):
        c = LatticeValue.const(5)
        self.assertEqual(lattice_meet(LatticeValue.top(), c), c)
        self.assertEqual(lattice_meet(c, LatticeValue.top()), c)

    def test_meet_same_constants(self):
        self.assertEqual(lattice_meet(LatticeValue.const(3),
                                      LatticeValue.const(3)),
                         LatticeValue.const(3))

    def test_meet_different_constants_is_bottom(self):
        m = lattice_meet(LatticeValue.const(1), LatticeValue.const(2))
        self.assertTrue(m.is_bottom())

    def test_meet_bottom_annihilates(self):
        for v in (LatticeValue.top(), LatticeValue.const(1),
                  LatticeValue.bottom()):
            self.assertTrue(lattice_meet(v, LatticeValue.bottom()).is_bottom())
            self.assertTrue(lattice_meet(LatticeValue.bottom(), v).is_bottom())

    def test_kinds(self):
        self.assertEqual(LatticeValue.top().kind, TOP)
        self.assertEqual(LatticeValue.bottom().kind, BOTTOM)
        self.assertEqual(LatticeValue.const(0).value, 0)


if __name__ == "__main__":
    unittest.main()
