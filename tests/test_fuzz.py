"""随机模糊交叉验证（固定种子，确定性、快速）。

对大量随机整数根多项式（含重根）与无有理根多项式，用 NumPy 独立结果交叉
核对：互异实根数、含重数总数、以及每个根是否被区间包围。

重根在浮点下病态（NumPy 可能算出带微小虚部/偏移的簇），因此：
- 精确整数根：直接断言其以精确点出现；
- 一般无平方因子多项式：用较宽松容差核对包围关系。
"""

from __future__ import annotations

import random
import unittest
from fractions import Fraction

from rootisolation import solve
from tests.helpers import numpy_real_roots, poly_from_roots


class TestFuzzCrossValidation(unittest.TestCase):
    def test_seeded_fuzz(self):
        rng = random.Random(20260924)
        n_trials = 120
        failures: list[str] = []

        for _ in range(n_trials):
            kind = rng.random()
            if kind < 0.5:
                k = rng.randint(1, 6)
                roots = rng.sample(range(-4, 5), k)
                coeffs = [str(c) for c in poly_from_roots(roots)]
                exact_roots = roots
            elif kind < 0.8:
                k = rng.randint(1, 5)
                roots = rng.choices(range(-3, 4), k=k)
                coeffs = [str(c) for c in poly_from_roots(roots)]
                exact_roots = sorted(set(roots))
            else:
                d = rng.randint(2, 6)
                cc = [rng.randint(-4, 4) for _ in range(d)] + [1]
                if all(x == 0 for x in cc[:-1]):
                    continue
                coeffs = [str(x) for x in cc]
                exact_roots = None  # 仅做根数 + 宽松包围核对

            r = solve({"coefficients": coeffs, "decimal_digits": 8})
            if r["status"] != "ok":
                failures.append(f"status={r['status']} for {coeffs}")
                continue

            np_roots = numpy_real_roots([Fraction(x) for x in coeffs])
            if r["summary"]["distinct_real_roots"] != len(np_roots):
                failures.append(
                    f"count {coeffs}: got "
                    f"{r['summary']['distinct_real_roots']}, numpy {np_roots}"
                )
                continue

            # 含重数总数必须等于次数
            s = r["summary"]
            self.assertEqual(
                s["real_roots_with_multiplicity"]
                + s["nonreal_complex_roots_with_multiplicity"],
                s["degree"],
            )

            if exact_roots is not None:
                got_exact = {Fraction(rt["interval"]["lower"])
                             for rt in r["roots"]
                             if rt["interval"]["exact"]}
                for z in exact_roots:
                    if Fraction(z) not in got_exact:
                        failures.append(f"missing exact root {z} in {coeffs}")
            else:
                for z in np_roots:
                    enclosed = False
                    for rt in r["roots"]:
                        a = Fraction(rt["interval"]["lower"])
                        b = Fraction(rt["interval"]["upper"])
                        if a == b:
                            if abs(float(a) - z) < 1e-6:
                                enclosed = True
                        elif float(a) - 1e-9 < z < float(b) + 1e-9:
                            enclosed = True
                    if not enclosed:
                        failures.append(f"{z} not enclosed in {coeffs}")

        self.assertEqual(failures, [], "\n".join(failures[:10]))

    def test_very_close_roots(self):
        # 间隔仅 10^-6 的相邻有理根必须被分开且各自精确
        p = poly_from_roots([0, Fraction(1, 1_000_000)])
        r = solve({"coefficients": [str(c) for c in p]})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["summary"]["distinct_real_roots"], 2)
        self.assertTrue(all(rt["interval"]["exact"] for rt in r["roots"]))


if __name__ == "__main__":
    unittest.main()
