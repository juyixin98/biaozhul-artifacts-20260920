"""增量解析引擎。

核心思想
========
1. 每次编辑后**重新词法分析整个文档**（词法分析线性且便宜，
   且能正确处理“编辑点在字符串/注释内部”这类情形）；
2. 用“旧令牌流 + 替换区间”算出旧令牌到新令牌的映射：
   替换区间左侧令牌按下标直映，右侧令牌从 EOF 反向按内容后缀对齐，
   与替换区间相交的令牌一律失效；
3. 用带 *复用上下文* 的同一个 Parser 重解析。每个解析例程入口
   先在旧解析表中查同入口、同例程的旧节点；满足以下条件则整棵复用：

   * 节点内每个旧令牌都映射为连续的新令牌；
   * 新旧令牌逐个相同（类型+原文）——编辑过的字符串内部符号必然失配；
   * 节点不含词法错误令牌（未闭合字符串等可能被后续编辑补全而改变）；
   * 节点（含后代）携带的所有错误锚点都能平移。

   复用节点的字符范围由其边界令牌在新文本中的位置决定，
   因此“节点范围更新正确”由构造保证，而非手工加减偏移量。

复用正确性不靠猜：差分测试把增量结果与同文本的全量解析逐字段
比较（规范化树 + 错误位置 + 消息）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .lexer import tokenize
from .parser import Parser, ParseResult, _ReuseCtx, parse


@dataclass(slots=True)
class Edit:
    """一次替换编辑：用 new_text 替换 [start, end)。

    插入：start == end；删除：new_text == ""。
    """

    start: int
    end: int
    new_text: str = ""

    def __post_init__(self):
        if self.start < 0 or self.end < self.start:
            raise ValueError("非法编辑区间")


@dataclass(slots=True)
class Document:
    """保持增量解析状态的文档句柄。"""

    source: str = ""
    result: ParseResult | None = None
    version: int = 0
    reuse_events: int = 0  # 最近一次解析复用的子树个数（统计用）

    def __post_init__(self):
        if self.result is None:
            self.result = parse(self.source)

    # ---------- 对外 API ----------

    def set_text(self, text: str) -> ParseResult:
        """把整个文档替换为 text（仍按一次替换编辑做增量解析）。"""
        return self.apply_edit(Edit(0, len(self.source), text))

    def apply_edit(self, edit: Edit) -> ParseResult:
        return self.apply_edits([edit])

    def apply_edits(self, edits: list[Edit]) -> ParseResult:
        """顺序应用多组编辑；每份中间文本都做一次增量解析，

        因此“连续编辑”与逐次调用 apply_edit 等价。"""
        text = self.source
        for ed in edits:
            if ed.end > len(text):
                raise ValueError("编辑越界")
            text = text[:ed.start] + ed.new_text + text[ed.end:]
            self._step(text, ed.start, ed.end, len(ed.new_text))
        return self.result

    # ---------- 内部 ----------

    def _step(self, new_source: str, rep_start: int, rep_end: int,
              ins_len: int):
        old = self.result
        new_tokens, new_lex_errors = tokenize(new_source)
        old_to_new, bad_old = build_token_map(
            old.tokens, new_tokens, rep_start, rep_end, ins_len,
        )
        new_to_old = {v: k for k, v in old_to_new.items()}
        ctx = _ReuseCtx(
            old_tokens=old.tokens,
            old_to_new=old_to_new,
            new_to_old=new_to_old,
            old_bad_toks=bad_old,
            old_entries=old.entries,
            max_old_id=old.max_node_id,
        )
        parser = Parser(new_source, new_tokens, new_lex_errors, reuse=ctx)
        reused = 0
        orig_register = parser.register_subtree

        def counting(new_node, old_node):
            nonlocal reused
            reused += 1
            return orig_register(new_node, old_node)

        parser.register_subtree = counting  # type: ignore[method-assign]
        self.result = parser.run()
        self.reuse_events = reused
        self.source = new_source
        self.version += 1


def build_token_map(old_tokens, new_tokens, rep_start, rep_end, ins_len):
    """构造 旧有效令牌/EOF下标 -> 新下标 的部分映射。

    * 旧侧边界：令牌整体位于替换区间左侧（end<=rep_start）或
      右侧（start>=rep_end）才可能映射；相交令牌失效。
    * 新侧边界：左侧令牌 start<rep_start；右侧令牌 end>rep_start+ins_len
      （插入时 rep_start==rep_end，恰好位于插入点两侧的令牌据此分开）。
    * 左侧从起点按下标直映；右侧从 EOF 反向按 (kind,text) 后缀对齐。
      两侧以位置边界封顶，内容再相似也不会越界重叠。
    * EOF 哨兵始终映射到新 EOF。
    """
    bad_old = {j for j, t in enumerate(old_tokens) if t.is_lex_error}
    new_ins_end = rep_start + ins_len
    mapping: dict[int, int] = {}
    used_new: set[int] = set()

    # ---- 前缀直映 ----
    p = 0
    while p < len(old_tokens) - 1 and p < len(new_tokens) - 1:
        ot, nt = old_tokens[p], new_tokens[p]
        if ot.end <= rep_start and nt.start < rep_start \
                and (ot.kind, ot.text) == (nt.kind, nt.text):
            mapping[p] = p
            used_new.add(p)
            p += 1
        else:
            break
    left_count = p

    # ---- 后缀对齐 ----
    oi = len(old_tokens) - 2  # 旧最后一个有效令牌
    ni = len(new_tokens) - 2
    while oi >= left_count and ni >= left_count:
        ot, nt = old_tokens[oi], new_tokens[ni]
        # 旧令牌必须整体位于替换区间右侧；新令牌必须整体位于新插入
        # 片段右侧。纯插入时 rep_end == rep_start，这一约束保证插入点
        # 左侧的旧令牌不会被错误地当作后缀对齐。
        if ot.start < rep_end or nt.start < new_ins_end:
            break
        if (ot.kind, ot.text) != (nt.kind, nt.text) or ni in used_new:
            # 同一新令牌不得同时映射两个旧令牌（编辑改变词法边界时会
            # 出现这种情况，例如插入引号改变字符串配对、多个旧 op
            # 合并成一个多字符运算符）
            break
        mapping[oi] = ni
        used_new.add(ni)
        oi -= 1
        ni -= 1

    # ---- EOF 哨兵 ----
    mapping[len(old_tokens) - 1] = len(new_tokens) - 1
    return mapping, bad_old
