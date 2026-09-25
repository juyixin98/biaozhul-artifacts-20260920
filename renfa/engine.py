"""NFA 模拟匹配引擎。

线性时间、无回溯
----------------
匹配模拟是集合驱动的 NFA 模拟：每个文本位置维护一个"可能处于的状态集合"，
一个码点只被消费一次，因此匹配耗时相对文本长度是线性的（每步成本为
``O(NFA状态数 × 出度)``，与文本无关）。**不存在回溯**，所以对
``(a+)+b`` 这类会让回溯引擎指数爆炸的模式不会退化。

Unicode 与锚点语义（见 README）
--------------------------------
* 输入按 **Unicode 码点** 切分，一个码点（含 emoji/汉字）算一个字符；
  引擎内部不做大小写折叠、规范化（NFC/NFD 原样比较码点）。
* ``.`` 匹配除 ``\\n``（U+000A）以外的任意单个码点。
* ``^`` 是零宽条件 ε 边，仅在码点位置 0 可通过（全文起始，无 MULTILINE）。
* ``$`` 仅在码点位置 len(text) 可通过（全文结尾；结尾 ``\\n`` **不**特殊，
  即 ``$`` 不会匹配末尾换行之前）。

搜索语义：左起始优先、同起始最长结束（POSIX 风格），与回溯型引擎按贪婪顺序
报告的结果在歧义情况下可能不同——本项目明确采用前者。
"""

from dataclasses import dataclass

from . import ast_nodes as ast
from .lexer import tokenize
from .nfa import NFA, build_nfa
from .parser import parse

_NEWLINE = 0x0A


@dataclass
class Match:
    start: int  # 码点偏移
    end: int    # 半开码点偏移
    text: str

    @property
    def span(self) -> tuple[int, int]:
        return self.start, self.end

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        return f"<Match span=({self.start},{self.end}) match={self.text!r}>"


class Regex:
    """编译后的正则模式，提供 fullmatch / match / search / findall。"""

    def __init__(self, pattern: str, max_states: int | None = None):
        self.pattern = pattern
        src, tokens = tokenize(pattern)
        self._source = src
        self.ast: ast.Node = parse(src, tokens)
        self.nfa: NFA = (
            build_nfa(self.ast)
            if max_states is None
            else build_nfa(self.ast, max_states=max_states)
        )
        self.last_steps = 0  # 上次匹配检查过的"状态/边"次数（供性能测试）

    # ---- ε 闭包 ----

    def _closure_set(self, states: set[int], pos: int, n: int) -> set[int]:
        """布尔模拟用：从 states 沿 ε/锚点边扩展。"""
        stack = list(states)
        seen = set(states)
        nfa = self.nfa
        while stack:
            s = stack.pop()
            for e in nfa.states[s].edges:
                if e.kind == "eps":
                    if e.target not in seen:
                        seen.add(e.target)
                        stack.append(e.target)
                elif e.kind == "anchor" and self._anchor_ok(e.anchor, pos, n):
                    if e.target not in seen:
                        seen.add(e.target)
                        stack.append(e.target)
        return seen

    def _closure_into(self, start_state: int, tag: int, pos: int, n: int,
                      active: dict[int, int]) -> None:
        """带标签模拟：从 start_state（标签 tag）扩展 ε/锚点边。

        active: {状态号 -> 到达它的最小起始位置（tag）}。
        若发现同状态已有更小/相等 tag 则该路被支配，无需继续；
        若带来更小 tag 则更新并重新出边传播。
        """
        stack = [(start_state, tag)]
        nfa = self.nfa
        while stack:
            s, t = stack.pop()
            old = active.get(s)
            if old is not None and old <= t:
                continue
            active[s] = t
            for e in nfa.states[s].edges:
                if e.kind == "eps":
                    stack.append((e.target, t))
                elif e.kind == "anchor" and self._anchor_ok(e.anchor, pos, n):
                    stack.append((e.target, t))

    @staticmethod
    def _anchor_ok(anchor: str | None, pos: int, n: int) -> bool:
        if anchor == "^":
            return pos == 0
        if anchor == "$":
            return pos == n
        return False

    # ---- 内部：四种判定 ----

    def _fullmatch(self, cps: list[int]) -> bool:
        """整串完全匹配。"""
        n = len(cps)
        active = self._closure_set({self.nfa.start}, 0, n)
        steps = 0
        for i, cp in enumerate(cps):
            nxt: set[int] = set()
            for s in active:
                for e in self.nfa.states[s].edges:
                    steps += 1
                    if e.kind == "char" and e.codepoint == cp:
                        nxt.add(e.target)
                    elif e.kind == "any" and cp != _NEWLINE:
                        nxt.add(e.target)
            active = self._closure_set(nxt, i + 1, n)
            if not active:
                self.last_steps = steps
                return False
        self.last_steps = steps
        return self.nfa.accept in active

    def _prefix_end(self, cps: list[int]) -> int | None:
        """锚定起始 0 的匹配；返回最长的结束位置（同起始最长语义）。"""
        n = len(cps)
        active = self._closure_set({self.nfa.start}, 0, n)
        result = 0 if self.nfa.accept in active else None
        for i, cp in enumerate(cps):
            nxt = set()
            for s in active:
                for e in self.nfa.states[s].edges:
                    if e.kind == "char" and e.codepoint == cp:
                        nxt.add(e.target)
                    elif e.kind == "any" and cp != _NEWLINE:
                        nxt.add(e.target)
            active = self._closure_set(nxt, i + 1, n)
            if self.nfa.accept in active:
                result = i + 1
        return result

    def _search(self, cps: list[int]) -> tuple[int, int] | None:
        """返回 (start, end)；左起始优先，同起始取最长结束。"""
        n = len(cps)
        active: dict[int, int] = {}
        self._closure_into(self.nfa.start, 0, 0, n, active)
        best: tuple[int, int] | None = None  # (start, -end) 比较用
        steps = 0
        i = 0
        while True:
            if self.nfa.accept in active:
                tag = active[self.nfa.accept]
                cand = (tag, -i)
                if best is None or cand < best:
                    best = cand
            if i != 0:
                # 在当前位置重新发起一次匹配（search 的每个起始点）
                self._closure_into(self.nfa.start, i, i, n, active)
                if self.nfa.accept in active:
                    tag = active[self.nfa.accept]
                    cand = (tag, -i)
                    if best is None or cand < best:
                        best = cand
            if i == n:
                break
            nxt: dict[int, int] = {}
            cp = cps[i]
            for s, tag in active.items():
                for e in self.nfa.states[s].edges:
                    steps += 1
                    if e.kind == "char" and e.codepoint == cp:
                        old = nxt.get(e.target)
                        if old is None or tag < old:
                            nxt[e.target] = tag
                    elif e.kind == "any" and cp != _NEWLINE:
                        old = nxt.get(e.target)
                        if old is None or tag < old:
                            nxt[e.target] = tag
            active = {}
            for s, tag in nxt.items():
                self._closure_into(s, tag, i + 1, n, active)
            i += 1
        self.last_steps = steps
        if best is None:
            return None
        return best[0], -best[1]

    # ---- 公开 API（参数与返回位置均为 Unicode 码点） ----

    def fullmatch(self, text: str) -> Match | None:
        cps = [ord(c) for c in text]
        if self._fullmatch(cps):
            return Match(0, len(cps), text)
        return None

    def match(self, text: str) -> Match | None:
        """锚定起始位置 0 的最长匹配（等价于 search 只从 0 出发）。"""
        cps = [ord(c) for c in text]
        end = self._prefix_end(cps)
        if end is None:
            return None
        return Match(0, end, text[:end])

    def search(self, text: str) -> Match | None:
        cps = [ord(c) for c in text]
        found = self._search(cps)
        if found is None:
            return None
        s, e = found
        return Match(s, e, text[s:e])

    def findall(self, text: str) -> list[Match]:
        """非重叠、左起始优先、同起始最长；零宽匹配后前进一个码点。"""
        results: list[Match] = []
        pos = 0
        n = len(text)
        while pos <= n:
            found = self._search([ord(c) for c in text[pos:]])
            if found is None:
                break
            s, e = found
            start, end = pos + s, pos + e
            results.append(Match(start, end, text[start:end]))
            pos = end if end > start else (start + 1 if start < n else n + 1)
            if start == n and end == n:
                break
        return results
