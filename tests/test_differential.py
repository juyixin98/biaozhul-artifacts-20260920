"""差分测试（验收核心）：随机插入/删除，增量解析必须与全量解析一致。

每个用例：

1. 从一组语法“积木”生成种子程序（含正确代码与畸形代码）；
2. 建立 Document（增量解析）；
3. 进行 N 次随机编辑（插入随机字符/片段、删除随机区间），
   每次编辑后用 :func:`dl.diff.assert_same` 比较：
     * 规范化树（kind/范围/tok 范围/text/子树）
     * 错误位置与消息（锚点 [start,end) + 文案）
4. 固定随机种子，保证可复现；任何不一致立即抛出并附带差异路径。

字符串内符号、未闭合括号、连续编辑等场景既在种子里覆盖，
也通过随机字符集（含 " ' ( ) ; { }）自然覆盖。
"""

import random
import unittest

from dl import Document, Edit
from dl.diff import assert_same

# 语法积木：部分合法、部分故意畸形
SEED_SNIPPETS = [
    "var a = 1 + 2 * 3;",
    "var b = (4 - 5) * 6;",
    "var s = \"he)llo {x};\";",
    "var t = 'unclosed;",
    "fn add(x, y) { return x + y; }",
    "fn bad(a b) { return a; }",
    "while (i < 10) { i = i + 1; break; }",
    "if (a) { x; } else if (b) { y; } else { z; }",
    "var u = (1 + 2;",
    "var v = f(1, g(2, 3), arr[4]);",
    "x = a[0] + b(;",
    ";;var empty;",
    "fn () { }",
    "var = ;",
    "return return;",
    "var q = !flag && (x || y);",
    "continue;",
    "var str = \"a\\nb\\tc\";",
    "// comment with ) ; { chars\nvar c = 1;",
    "var unclosed = \"never ends",
]

# 随机插入字符集：刻意混入括号、引号、分号等
ALPHABET = list("abcxyz019 +-*/(){};,\"'[]<>=!\n\t")
WORDS = ["var", "fn", "return", "if", "else", "while", "123", "name",
         "\"str)\"", "(1 + 2)", "{ x; }", "();", ";;"]


def random_edit(rng: random.Random, source: str) -> Edit:
    if not source or rng.random() < 0.55:
        # 插入
        pos = rng.randrange(len(source) + 1)
        if rng.random() < 0.65:
            text = "".join(rng.choice(ALPHABET)
                           for _ in range(rng.randrange(1, 5)))
        else:
            text = rng.choice(WORDS)
        return Edit(pos, pos, text)
    # 删除（有时替换为短文本，即“删除+插入”复合编辑）
    pos = rng.randrange(len(source))
    end = min(len(source), pos + rng.randrange(1, 6))
    if rng.random() < 0.3:
        return Edit(pos, end, rng.choice(");\"x\n "))
    return Edit(pos, end, "")


def run_sequence(seed: int, seed_source: str, steps: int) -> int:
    """跑一条编辑序列，返回过程中至少一次发生复用的步骤数。"""
    rng = random.Random(seed)
    doc = Document(source=seed_source)
    assert_same(_full(seed_source), doc.result)
    reused_steps = 0
    for _ in range(steps):
        edit = random_edit(rng, doc.source)
        doc.apply_edit(edit)
        # 每一步：同文本全量解析必须与增量结果一致
        assert_same(_full(doc.source), doc.result)
        if doc.reuse_events > 0:
            reused_steps += 1
    return reused_steps


def _full(source: str):
    from dl.parser import parse
    return parse(source)


class TestDifferential(unittest.TestCase):
    def test_many_random_walks(self):
        """30 个种子 × 40 步随机编辑，逐步与全量解析比对。"""
        total_reused_steps = 0
        for i, snippet in enumerate(SEED_SNIPPETS):
            reused = run_sequence(seed=1000 + i, seed_source=snippet, steps=40)
            total_reused_steps += reused
        # 健全性检查：随机游走中应当真的发生过节点复用，
        # 否则测试即使通过也没有验证“复用后仍正确”
        self.assertGreater(total_reused_steps, 100,
                           f"复用发生次数过少: {total_reused_steps}")

    def test_random_seed_programs(self):
        """用积木随机拼接更大的种子程序，再做随机编辑。"""
        for seed in range(20):
            rng = random.Random(seed)
            lines = rng.sample(SEED_SNIPPETS,
                               k=rng.randrange(3, len(SEED_SNIPPETS)))
            source = "\n".join(lines)
            run_sequence(seed=5000 + seed, seed_source=source, steps=25)

    def test_string_interior_specifically(self):
        """在字符串内部反复插入括号/引号，必须与全量解析一致。"""
        doc = Document(source='var s = "abc";\nvar t = "(x);";\n')
        rng = random.Random(77)
        for _ in range(60):
            p = rng.randrange(len(doc.source) + 1)
            doc.apply_edit(Edit(p, p, rng.choice(")('\";")))
            assert_same(_full(doc.source), doc.result)

    def test_unclosed_bracket_storm(self):
        """反复增删括号：未闭合/刚闭合状态来回切换。"""
        doc = Document(source="var a = (1 + 2) * 3;\nfn f(x) { return x; }\n")
        rng = random.Random(99)
        for _ in range(80):
            if rng.random() < 0.5:
                p = rng.randrange(len(doc.source) + 1)
                doc.apply_edit(Edit(p, p, rng.choice("([{}])")))
            else:
                p = rng.randrange(len(doc.source))
                doc.apply_edit(Edit(p, p + 1, ""))
            assert_same(_full(doc.source), doc.result)

    def test_long_session_reuse_equivalence(self):
        """单个文档上 200 次连续编辑（会话场景）。"""
        run_sequence(seed=42,
                     seed_source="var x = 1;\nfn f(a) { return a; }\n",
                     steps=200)


if __name__ == "__main__":
    unittest.main()
