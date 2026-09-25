"""穷举等价测试（核心验收项之一）。

对由受限语法生成的大量模式：
  1. 在字母表 {a,b} 的全部长度 0..N 字符串上，比较 Thompson 引擎与朴素
     回溯参考解释器的 fullmatch 布尔结果、search 的 (start,end)；
  2. 无锚点子集再与 Python 标准库 re 交叉验证 fullmatch。

参考解释器带步数预算；若某个模式在参考侧预算耗尽（嵌套重复导致的指数展开），
不计为分歧，仅统计并跳过——那类模式由 test_catastrophic 专门处理。
"""

import itertools
import re as pyre
import unittest

from renfa import Regex, reference
from renfa.reference import BudgetExhausted

# ---- 模式生成 ----

LITERALS = ["a", "b", "."]
BOUNDED_QUANTS = ["", "{0}", "{1}", "{2}", "{0,1}", "{1,2}", "{0,2}"]


def gen_patterns(depth: int) -> set[str]:
    """生成覆盖 连接/选择/括号/有限重复 的模式集合（字符串去重）。"""
    if depth == 0:
        return set(LITERALS)
    sub = gen_patterns(depth - 1)
    sub_list = sorted(sub)
    out: set[str] = set(LITERALS)
    # 给子模式加有限重复（括号包裹，避免量词直接叠加的非法语法）
    for p in sub_list:
        for q in BOUNDED_QUANTS:
            out.add(f"({p}){q}")
    # 连接与选择（只在较短模式上组合，控制 NFA 规模与总量）
    small = [p for p in sub_list if len(p) <= 5][:8]
    for p in small:
        for q in small:
            out.add(p + q)
            out.add(f"{p}|{q}")
            out.add(f"({p}|{q})")
    return out


def all_strings(alphabet: str, max_len: int):
    for n in range(max_len + 1):
        for tup in itertools.product(alphabet, repeat=n):
            yield "".join(tup)


PATTERNS = sorted(gen_patterns(2))
TEXTS = list(all_strings("ab", 4))


class ExhaustiveEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.compared = 0
        self.skipped_budget = 0

    def test_fullmatch_matches_reference(self):
        failures = []
        for pat in PATTERNS:
            try:
                eng = Regex(pat)
            except Exception as e:  # noqa: BLE001 - 测试里要暴露意外失败
                failures.append(f"编译 {pat!r} 抛异常: {e!r}")
                continue
            for text in TEXTS:
                try:
                    ref_ok = reference.fullmatch(pat, text, budget=300_000)
                except BudgetExhausted:
                    self.skipped_budget += 1
                    continue
                self.compared += 1
                eng_ok = eng.fullmatch(text) is not None
                if eng_ok != ref_ok:
                    failures.append(f"fullmatch({pat!r}, {text!r}): "
                                    f"engine={eng_ok} reference={ref_ok}")
        self._report(failures)

    def test_search_matches_reference(self):
        failures = []
        for pat in PATTERNS:
            try:
                eng = Regex(pat)
            except Exception as e:  # noqa: BLE001
                failures.append(f"编译 {pat!r} 抛异常: {e!r}")
                continue
            for text in TEXTS:
                try:
                    ref = reference.search(pat, text, budget=300_000)
                except BudgetExhausted:
                    self.skipped_budget += 1
                    continue
                self.compared += 1
                m = eng.search(text)
                eng_span = None if m is None else (m.start, m.end)
                if eng_span != ref:
                    failures.append(f"search({pat!r}, {text!r}): "
                                    f"engine={eng_span} reference={ref}")
        self._report(failures)

    def test_fullmatch_matches_python_re(self):
        """无锚点子集：本引擎与 Python re 的 fullmatch 布尔结果应一致。"""
        failures = []
        checked = 0
        for pat in PATTERNS:
            if "^" in pat or "$" in pat:
                continue
            eng = Regex(pat)
            for text in TEXTS:
                py_ok = pyre.fullmatch(pat, text, pyre.DOTALL) is not None
                eng_ok = eng.fullmatch(text) is not None
                checked += 1
                if eng_ok != py_ok:
                    failures.append(f"fullmatch({pat!r}, {text!r}): "
                                    f"engine={eng_ok} python_re={py_ok}")
        if failures:
            self.fail("\n".join(failures[:20])
                      + f"\n...共 {len(failures)} 个分歧，比较 {checked} 对")
        # 确保交叉验证确实跑了足够多的样本
        self.assertGreater(checked, 5000)

    def _report(self, failures):
        if failures:
            self.fail(
                "\n".join(failures[:20])
                + f"\n...共 {len(failures)} 个分歧；"
                f"实际比较 {self.compared} 对，"
                f"参考侧预算耗尽跳过 {self.skipped_budget} 对"
            )


if __name__ == "__main__":
    unittest.main()
