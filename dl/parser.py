"""递归下降语法分析器（Pratt 表达式）。

全量解析与增量解析共用本文件的全部解析例程：

* ``parse(source)`` 做一次普通全量解析；
* 增量解析（见 :mod:`dl.incremental`）给 Parser 传入一个 *复用上下文*，
  每个解析例程入口先查旧解析表，命中且节点可复用时直接克隆旧节点、
  跳过其令牌区间，否则照常解析。两条路径语法逻辑完全相同，
  因此随机差分测试可以用“全量解析”做正确性标尺。

错误归属
========
每个解析例程用一个 *错误桶*（owner bucket）收集“本层”产生的诊断；
嵌套例程有自己的桶并自行取走，因此每一条语法错误恰好归属于产生它的
最深层节点。增量复用子树时，连同后代桶中的错误一起克隆并平移位置。

错误恢复策略（确定性，保证全量/增量结果一致）：

* 结构性错误（缺少 ``( ) { }``、if/while/函数头损坏等）
  → *垃圾同步*：按括号深度消费到下一个 ``;``，生成 Junk 节点；
* 缺少分号 → 报错但不消费令牌（dangling 边界错误）；
* 运算符后缺右值 → 报错并返回已解析部分（dangling 错误）；
* 未闭合括号 → 错误锚在 EOF 或下一个令牌，不消费，继续向后解析。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .lexer import Token, tokenize
from .nodes import Node, ParseError

# 运算符左结合优先级（数值越大结合越紧）；赋值为右结合、优先级最低
ASSIGN_P = 0
LBP = {
    "=": 0,
    "||": 1, "&&": 2,
    "==": 3, "!=": 3, "<": 3, ">": 3, "<=": 3, ">=": 3,
    "+": 4, "-": 4,
    "*": 5, "/": 5, "%": 5,
}
RIGHT_ASSOC = {"="}
PREFIX_BP = 6
PREFIX_OPS = {"-", "!"}


@dataclass(slots=True)
class ParseResult:
    source: str
    tree: Node
    errors: list[ParseError] = field(default_factory=list)
    tokens: list[Token] = field(default_factory=list)
    # 解析表：pkey -> {入口令牌下标: 节点}
    # 表达式表的 pkey 形如 "expr:<minbp>"，其余为例程名。
    entries: dict[str, dict[int, Node]] = field(default_factory=dict)
    max_node_id: int = 0


def parse(source: str) -> ParseResult:
    """全量解析入口。"""
    tokens, lex_errors = tokenize(source)
    return Parser(source, tokens, lex_errors).run()


class _ReuseCtx:
    """增量解析复用上下文（由 incremental.Document 构造）。"""

    __slots__ = (
        "old_tokens", "old_to_new", "new_to_old", "old_bad_toks",
        "entries", "next_id",
    )

    def __init__(self, old_tokens, old_to_new, new_to_old, old_bad_toks,
                 old_entries, max_old_id):
        self.old_tokens = old_tokens
        self.old_to_new = old_to_new
        self.new_to_old = new_to_old
        self.old_bad_toks = old_bad_toks
        self.entries = old_entries
        self.next_id = max_old_id + 1


def _starts_expr(t: Token) -> bool:
    if t.kind in ("number", "string", "ident"):
        return True
    return t.text in ("(", "[", "-", "!", "true", "false", "null")


class Parser:
    def __init__(self, source, tokens, lex_errors, reuse: _ReuseCtx | None = None):
        self.source = source
        self.tokens = tokens
        self.reuse = reuse
        self.pos = 0
        self.errors: list[ParseError] = list(lex_errors)
        self.entries: dict[str, dict[int, Node]] = defaultdict(dict)
        self.owners: list[list[ParseError]] = []
        self._next_id = reuse.next_id if reuse else 1
        # 已被复用子树消费的新令牌集合（按令牌下标）。
        # 父节点整体克隆时会一并消费后代；此后任何只覆盖其中一部分的
        # 复用命中（嵌套表项）都必须拒绝，否则同一节点/错误会重复。
        self._consumed_toks: set[int] = set()

    # ---------- 基础设施 ----------

    def run(self) -> ParseResult:
        tree = self.parse_program()
        # 规范化错误顺序：按（起点, 终点, 消息）排序，消除产生顺序差异
        self.errors.sort(key=lambda e: (e.start, e.end, e.message))
        return ParseResult(
            source=self.source, tree=tree, errors=self.errors,
            tokens=self.tokens, entries=dict(self.entries),
            max_node_id=max(self._next_id - 1, 0),
        )

    @property
    def cur(self) -> Token:
        return self.tokens[self.pos]

    def peek_text(self) -> str:
        return self.tokens[self.pos].text

    def advance(self) -> Token:
        t = self.tokens[self.pos]
        self.pos += 1
        return t

    def mk(self, kind, tok_start_idx, tok_end_idx, *, text=None,
           children=None) -> Node:
        """用令牌下标构造节点（半开区间 [tok_start_idx, tok_end_idx)）。"""
        ts = self.tokens[tok_start_idx]
        end_idx = tok_end_idx - 1
        if tok_end_idx > tok_start_idx:
            end = self.tokens[end_idx].end
        else:
            end = ts.start  # 零宽度节点：位置挂在边界令牌上
        return Node(
            kind=kind, start=ts.start, end=end,
            tok_start=tok_start_idx, tok_end=tok_end_idx,
            text=text, children=children or [],
            node_id=self._get_id(),
        )

    def _get_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    def begin(self) -> list[ParseError]:
        bucket: list[ParseError] = []
        self.owners.append(bucket)
        return bucket

    def finish(self, bucket, node, pkey):
        """例程收尾：绑定错误桶、登记解析表、弹桶。"""
        node.owned_errors = bucket
        node.pkey = pkey
        if self.owners:
            self.owners.pop()
        self.entries[pkey][node.tok_start] = node
        return node

    def emit(self, message: str, tok: Token) -> ParseError:
        err = ParseError(message, tok.start, tok.end)
        self.errors.append(err)
        if self.owners:
            self.owners[-1].append(err)
        return err

    # ---------- 增量复用 ----------

    def try_reuse(self, pkey: str, start_idx: int):
        r = self.reuse
        if r is None:
            return None
        # 当前新位置先反向映射到旧下标，再查旧解析表
        old_start = r.new_to_old.get(start_idx)
        if old_start is None:
            return None
        old = r.entries.get(pkey, {}).get(old_start)
        if old is None or not self.eligible(old):
            return None
        # 旧节点映射后的起点必须正好等于当前解析位置
        mapped_start = r.old_to_new.get(old.tok_start)
        if mapped_start != start_idx:
            return None
        mapped_range = range(r.old_to_new[old.tok_start],
                             r.old_to_new[old.tok_end - 1] + 1)
        # 复用区间内的每个新令牌都必须尚未被消费。父节点整棵克隆时
        # 后代是随父克隆的，不经过这里；此检查拒绝的是“父已整体克隆，
        # 同一后代又以独立表项被命中”的重复消费。
        if any(k in self._consumed_toks for k in mapped_range):
            return None
        new_end = r.old_to_new[old.tok_end - 1] + 1
        # 表达式节点的复用守卫：编辑可能在节点尾后插入了会扩展该
        # 表达式的令牌（postfix 调用 '('，或优先级不低于本层的中缀
        # 运算符）。有则必须重解析以把扩展部分纳入。
        if pkey.startswith("expr:") and self._extends_expr(
                pkey, new_end):
            return None
        # 块节点的复用守卫：旧块尾后紧接 '}'。编辑可能在块尾插入了
        # 新语句，此时新流中映射的 '}' 与块最后一个令牌之间多出令牌。
        if pkey == "block" and not self._block_close_intact(old, new_end):
            return None
        cloned = self.clone_node(old)
        self.pos = cloned.tok_end
        self._consumed_toks.update(mapped_range)
        self.register_subtree(cloned, old)
        return cloned

    def _extends_expr(self, pkey: str, new_end: int) -> bool:
        """新流 new_end 处的令牌是否会继续扩展该表达式？"""
        if new_end >= len(self.tokens) - 1:
            return False  # 后面是 EOF
        t = self.tokens[new_end]
        if t.text == "(":
            return True   # 新增的 postfix 调用
        min_bp = int(pkey.split(":", 1)[1])
        return t.kind == "op" and t.text in LBP and LBP[t.text] >= min_bp

    def _block_close_intact(self, old: Node, new_end: int) -> bool:
        """闭合块复用守卫。

        Block 的最后一个消费令牌是 '}'。旧块体末尾令牌与 '}' 在旧流中
        相邻；若编辑在块尾插入了新语句，则新流中二者之间多出令牌，
        此时不得复用（否则会漏掉插入的语句）。
        """
        r = self.reuse
        close_old = old.tok_end - 1
        if r.old_tokens[close_old].text != "}":
            return True  # 未闭合块：携带错误，eligible 已拒绝
        # Block 至少含 '{' 与 '}'
        body_last_old = close_old - 1
        new_close = r.old_to_new.get(close_old)
        new_body_last = r.old_to_new.get(body_last_old)
        if new_close is None:
            return False
        if new_body_last is not None:
            # 正常情形：新 '}' 必须紧接新块体末令牌
            return new_close == new_body_last + 1
        # 块体为空（只有 '{' '}'）：新 '}' 必须紧接新 '{'
        open_old = old.tok_start
        new_open = r.old_to_new.get(open_old)
        return new_open is not None and new_close == new_open + 1

    def register_subtree(self, new_node: Node, old_node: Node):
        """把复用子树登记进新解析表，供后续编辑继续复用。"""
        if old_node.pkey:
            self.entries[old_node.pkey][new_node.tok_start] = new_node
        for nc, oc in zip(new_node.children, old_node.children):
            self.register_subtree(nc, oc)

    @staticmethod
    def walk_errors(node: Node):
        yield from node.owned_errors
        for c in node.children:
            yield from Parser.walk_errors(c)

    def eligible(self, old: Node) -> bool:
        """旧节点可否复用。判据：

        1. 非零宽度节点；
        2. 节点消费的每个旧令牌都映射为连续、同内容的新令牌，
           且其中没有词法错误令牌（未闭合字符串等可能被补全）；
        3. **节点（含任意后代）不携带任何语法错误。**

        第 3 条是关键简化：错误恢复产生的树结构与错误锚点依赖编辑点
        上下文，迁移容易出错；而含错误的节点本就处于“受影响”状态。
        拒绝复用后，这些错误由重解析区域重新产生，位置天然正确。
        错误文档中未受影响的干净声明仍会照常复用。
        """
        r = self.reuse
        if old.tok_start >= old.tok_end:
            return False  # 零宽度节点（空包裹等）不直接复用
        mapped: list[int] = []
        for j in range(old.tok_start, old.tok_end):
            nj = r.old_to_new.get(j)
            if nj is None:
                return False
            mapped.append(nj)
        first = mapped[0]
        if mapped != list(range(first, first + len(mapped))):
            return False
        for oj, nj in zip(range(old.tok_start, old.tok_end), mapped):
            ot, nt = r.old_tokens[oj], self.tokens[nj]
            if (ot.kind, ot.text) != (nt.kind, nt.text):
                return False
            if j_bad(ot) or j_bad(nt):
                return False
        # 携带任何错误（含后代）的节点一律重解析
        for _ in self.walk_errors(old):
            return False
        return True

    def clone_node(self, old: Node) -> Node:
        """按令牌映射整体平移复制旧节点（含干净子节点；不含错误）。"""
        r = self.reuse
        children = [self.clone_node(c) for c in old.children]
        if old.tok_start < old.tok_end:
            new_ts = r.old_to_new[old.tok_start]
            new_te = r.old_to_new[old.tok_end - 1] + 1
            start = self.tokens[new_ts].start
            end = self.tokens[new_te - 1].end
            anchor = None
        else:
            # 零宽度包裹节点（空 ParamList/ArgList）：锚点是前导
            # 开括号，映射后位置取新开括号末尾
            old_anchor = old.tok_anchor if old.tok_anchor is not None \
                else max(old.tok_start - 1, 0)
            anchor_new = r.old_to_new[old_anchor]
            new_ts = new_te = anchor_new + 1
            start = end = self.tokens[anchor_new].end
            anchor = anchor_new
        node = Node(
            kind=old.kind, start=start, end=end,
            tok_start=new_ts, tok_end=new_te,
            text=old.text, children=children,
            owned_errors=[], node_id=old.node_id,
            tok_anchor=anchor,
        )
        node.pkey = old.pkey
        return node

    def _new_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    # ---------- 错误恢复 ----------

    def sync_to_semi(self, *, in_block: bool) -> int:
        """按括号深度消费令牌直到最外层 ';'（含），返回消费后位置。

        块内遇到最外层 '}' 停下且不消费（块结束符留给上层）；
        游离的 ')' / ']' 属于垃圾的一部分，直接消费以免死循环。
        """
        depth = 0
        while self.cur.kind != "eof":
            t = self.cur
            if t.text in "([{":
                depth += 1
                self.advance()
            elif t.text in ")]}":
                if depth == 0:
                    if in_block and t.text == "}":
                        return self.pos
                    # 游离右括号/方括号：消费掉，保证恢复一定前进
                    self.advance()
                else:
                    depth -= 1
                    self.advance()
            elif t.text == ";" and depth == 0:
                self.advance()
                return self.pos
            else:
                self.advance()
        return self.pos

    def junk(self, start_idx: int, message: str, *, in_block: bool) -> Node:
        """结构性错误恢复：报错并同步到分号，生成 Junk 节点。

        全局错误列表始终完整（emit 即记录）；这里额外把同步期间深层
        例程产生的错误在 owned_errors 层面归并给 Junk，便于 JSON 展示
        错误与恢复区间的对应关系。增量复用只认干净节点，含错节点一律
        重解析，因此错误集合的正确性不依赖此处归属。
        """
        bucket = self.begin()
        before = len(self.errors)
        self.emit(message, self.cur)
        end_idx = self.sync_to_semi(in_block=in_block)
        if end_idx == start_idx:
            end_idx = start_idx + 1  # 至少占住锚点令牌（EOF 时为 EOF 位）
        node = self.mk("Junk", start_idx, end_idx)
        for err in self.errors[before + 1:]:
            if err not in bucket:
                bucket.append(err)
        return self.finish(bucket, node, "junk")

    # ---------- 程序 / 块 ----------

    def parse_program(self) -> Node:
        # Program 始终整体重解析（其声明子节点各自复用）：它逻辑上
        # 覆盖到 EOF，末尾插入/删除令牌时边界会变，直接复用不安全。
        bucket = self.begin()
        start_idx = 0
        decls: list[Node] = []
        while self.cur.kind != "eof":
            if self.peek_text() == "}":
                # 游离的块结束符：按语句垃圾恢复，保证主循环一定前进
                decls.append(self.junk(self.pos, "多余的 '}'",
                                       in_block=False))
                continue
            d = self.parse_toplevel()
            if d is not None:
                decls.append(d)
            else:
                # 理论上不可达：兜底消费一个令牌防止死循环
                decls.append(self.junk(self.pos, "语法错误：无法解析的语句",
                                       in_block=False))
        node = self.mk("Program", start_idx, len(self.tokens) - 1,
                       children=decls)
        node.start = 0
        node.end = len(self.source)
        node.tok_end = len(self.tokens) - 1
        return self.finish(bucket, node, "program")

    def parse_toplevel(self) -> Node | None:
        if self.peek_text() == "fn":
            return self.parse_fn()
        return self.parse_stmt(in_block=False, allow_decl=True)

    def parse_block(self) -> Node | None:
        start_idx = self.pos
        if (n := self.try_reuse("block", start_idx)) is not None:
            return n
        if self.peek_text() != "{":
            return None
        bucket = self.begin()
        self.advance()  # {
        stmts: list[Node] = []
        while self.cur.kind != "eof" and self.peek_text() != "}":
            s = self.parse_stmt(in_block=True, allow_decl=True)
            if s is not None:
                stmts.append(s)
        if self.cur.kind == "eof":
            self.emit("未闭合的语句块，缺少 '}'", self.cur)  # 锚在 EOF
            end_idx = self.pos  # EOF 哨兵不纳入令牌区间
        else:
            self.advance()  # }
            end_idx = self.pos
        node = self.mk("Block", start_idx, end_idx, children=stmts)
        return self.finish(bucket, node, "block")

    def parse_stmt(self, *, in_block: bool, allow_decl: bool) -> Node | None:
        t = self.cur
        if t.kind == "eof" or t.text == "}":
            return None
        if allow_decl and t.text == "var":
            return self.parse_var(in_block=in_block)
        if t.text == ";":
            return self.parse_empty()
        if t.text == "{":
            return self.parse_block()
        if t.text == "if":
            return self.parse_if(in_block=in_block)
        if t.text == "while":
            return self.parse_while(in_block=in_block)
        if t.text == "return":
            return self.parse_return(in_block=in_block)
        if t.text in ("break", "continue"):
            return self.parse_simple(t.text, in_block=in_block)
        if _starts_expr(t):
            return self.parse_expr_stmt(in_block=in_block)
        return self.junk(self.pos, "语法错误：无法解析的语句", in_block=in_block)

    def parse_empty(self) -> Node:
        start_idx = self.pos
        if (n := self.try_reuse("empty", start_idx)) is not None:
            return n
        bucket = self.begin()
        self.advance()  # ;
        node = self.mk("Empty", start_idx, self.pos)
        return self.finish(bucket, node, "empty")

    # ---------- 声明 ----------

    def parse_var(self, *, in_block: bool) -> Node:
        start_idx = self.pos
        if (n := self.try_reuse("var", start_idx)) is not None:
            return n
        self.advance()  # var
        if self.cur.kind != "ident":
            return self.junk(start_idx, "var 声明缺少变量名", in_block=in_block)
        bucket = self.begin()
        name = self.leaf("Ident", start_idx + 1)
        self.advance()
        children: list[Node] = [name]
        if self.peek_text() == "=":
            self.advance()
            if _starts_expr(self.cur):
                children.append(self.parse_expr(0))
            else:
                self.emit("变量声明缺少初始化表达式", self.cur)
        node = self.finish_stmt(bucket, "VarDecl", start_idx,
                                children=children, pkey="var", in_block=in_block)
        return node

    def parse_fn(self) -> Node:
        start_idx = self.pos
        if (n := self.try_reuse("fn", start_idx)) is not None:
            return n
        self.advance()  # fn
        if self.cur.kind != "ident":
            return self.junk(start_idx, "fn 声明缺少函数名", in_block=False)
        name_idx = self.pos
        self.advance()
        if self.peek_text() != "(":
            return self.junk(start_idx, "函数声明缺少参数列表 '('",
                             in_block=False)
        bucket = self.begin()
        name = self.leaf("Ident", name_idx)
        self.advance()  # (
        params, header_broken = self.parse_param_list()
        if self.peek_text() == ")":
            self.advance()
        else:
            self.emit("未闭合的参数列表，缺少 ')'", self.cur)
        body = None
        if not header_broken and self.peek_text() == "{":
            body = self.parse_block()
        elif not header_broken:
            self.emit("函数声明缺少函数体 '{'", self.cur)
            self.sync_to_semi(in_block=False)
        else:
            # 头部已损坏：同步到分号，尽力吃掉残余（可能含 '{' 块）
            self.sync_to_semi(in_block=False)
        plist = self.wrap("ParamList", params)
        children = [name, plist]
        if body is not None:
            children.append(body)
        node = self.mk("FnDecl", start_idx, self.pos, children=children)
        return self.finish(bucket, node, "fn")

    def parse_param_list(self) -> tuple[list[Node], bool]:
        params: list[Node] = []
        while True:
            t = self.cur
            if t.text == ")" or t.kind == "eof":
                return params, False
            if t.text == "{" or t.text == ";":
                self.emit("参数列表解析中断，缺少 ')'", t)
                return params, True
            if t.kind == "ident":
                params.append(self.leaf("Ident", self.pos))
                self.advance()
                if self.peek_text() == ",":
                    self.advance()
                    continue
                if self.peek_text() == ")":
                    return params, False
                self.emit("参数列表中缺少 ',' 或 ')'", self.cur)
                self.skip_to_header_end()
                return params, True
            self.emit("参数应为标识符", self.cur)
            self.skip_to_header_end()
            return params, True

    def skip_to_header_end(self):
        """参数/实参列表中段损坏：跳过残余直到同层 ')' '{' ';' 或 '}'。"""
        depth = 0
        while self.cur.kind != "eof":
            x = self.cur
            if x.text in "([{":
                depth += 1
            elif x.text in ")]}":
                if depth == 0:
                    return
                depth -= 1
            elif depth == 0 and x.text in (";", "}"):
                return
            self.advance()

    # ---------- 控制流语句 ----------

    def parse_if(self, *, in_block: bool) -> Node:
        start_idx = self.pos
        if (n := self.try_reuse("if", start_idx)) is not None:
            return n
        self.advance()  # if
        if self.peek_text() != "(":
            return self.junk(start_idx, "if 语句缺少条件 '('",
                             in_block=in_block)
        self.advance()
        cond = self.parse_expr(0)
        if self.peek_text() != ")":
            return self.junk(start_idx, "未闭合的条件，缺少 ')'",
                             in_block=in_block)
        self.advance()
        if self.peek_text() != "{":
            return self.junk(start_idx, "if 语句缺少语句块", in_block=in_block)
        bucket = self.begin()
        then_block = self.parse_block()
        children = [cond, then_block]
        if self.peek_text() == "else":
            self.advance()
            if self.peek_text() == "{":
                children.append(self.wrap("Else", [self.parse_block()]))
            elif self.peek_text() == "if":
                children.append(self.wrap("Else",
                                         [self.parse_if(in_block=in_block)]))
            else:
                # else 损坏：先 finish if（错误归 else），再恢复
                self.emit("else 后缺少语句块或 if", self.cur)
                node = self.mk("If", start_idx, self.pos, children=children)
                self.finish(bucket, node, "if")
                return self.junk(self.pos, "else 子句损坏",
                                 in_block=in_block)
        node = self.mk("If", start_idx, self.pos, children=children)
        return self.finish(bucket, node, "if")

    def parse_while(self, *, in_block: bool) -> Node:
        start_idx = self.pos
        if (n := self.try_reuse("while", start_idx)) is not None:
            return n
        self.advance()
        if self.peek_text() != "(":
            return self.junk(start_idx, "while 语句缺少条件 '('",
                             in_block=in_block)
        self.advance()
        cond = self.parse_expr(0)
        if self.peek_text() != ")":
            return self.junk(start_idx, "未闭合的条件，缺少 ')'",
                             in_block=in_block)
        self.advance()
        if self.peek_text() != "{":
            return self.junk(start_idx, "while 语句缺少语句块",
                             in_block=in_block)
        bucket = self.begin()
        body = self.parse_block()
        node = self.mk("While", start_idx, self.pos, children=[cond, body])
        return self.finish(bucket, node, "while")

    def parse_return(self, *, in_block: bool) -> Node:
        start_idx = self.pos
        if (n := self.try_reuse("re", start_idx)) is not None:
            return n
        bucket = self.begin()
        self.advance()  # return
        children: list[Node] = []
        if _starts_expr(self.cur):
            children.append(self.parse_expr(0))
        return self.finish_stmt(bucket, "Return", start_idx,
                                children=children, pkey="re", in_block=in_block)

    def parse_simple(self, keyword: str, *, in_block: bool) -> Node:
        start_idx = self.pos
        if (n := self.try_reuse(keyword, start_idx)) is not None:
            return n
        bucket = self.begin()
        self.advance()  # break / continue
        kind = "Break" if keyword == "break" else "Continue"
        return self.finish_stmt(bucket, kind, start_idx, pkey=keyword,
                                in_block=in_block)

    def parse_expr_stmt(self, *, in_block: bool) -> Node:
        start_idx = self.pos
        if (n := self.try_reuse("exprstmt", start_idx)) is not None:
            return n
        bucket = self.begin()
        children: list[Node] = []
        if _starts_expr(self.cur):
            children.append(self.parse_expr(0))
        return self.finish_stmt(bucket, "ExprStmt", start_idx,
                                children=children, pkey="exprstmt",
                                in_block=in_block)

    def finish_stmt(self, bucket, kind, start_idx, *, children=None, pkey,
                    in_block: bool) -> Node:
        """语句公共收尾：处理尾随 ';'（缺失则报边界错误且不消费）。"""
        if self.peek_text() == ";":
            self.advance()
        else:
            self.emit("缺少 ';'", self.cur)
        node = self.mk(kind, start_idx, self.pos,
                       children=children if children is not None else [])
        return self.finish(bucket, node, pkey)

    # ---------- 表达式（Pratt） ----------

    def parse_expr(self, min_bp: int) -> Node:
        start_idx = self.pos
        pkey = f"expr:{min_bp}"
        if (n := self.try_reuse(pkey, start_idx)) is not None:
            return n
        bucket = self.begin()
        left = self.parse_prefix()
        if left.kind == "ErrorExpr":
            # 无法开始表达式：返回零宽度 ErrorExpr（错误已在桶中）。
            # 该节点携带错误，增量解析时不会被复用。
            return self.finish(bucket, left, pkey)
        while self.cur.kind == "op" and self.cur.text in LBP \
                and LBP[self.cur.text] >= min_bp:
            op_tok = self.advance()
            if op_tok.text in RIGHT_ASSOC:
                rhs_min = LBP[op_tok.text]       # 右结合
            else:
                rhs_min = LBP[op_tok.text] + 1   # 左结合
            right = self.parse_expr(rhs_min)
            if right.kind == "ErrorExpr":
                kind = ("右操作数" if op_tok.text != "=" else "赋值表达式")
                self.emit(f"运算符 {op_tok.text!r} 缺少{kind}", op_tok)
                break
            node_kind = "Assign" if op_tok.text == "=" else "Binary"
            left = self.mk(node_kind, start_idx, self.pos,
                           text=op_tok.text, children=[left, right])
        return self.finish(bucket, left, pkey)

    def parse_prefix(self) -> Node:
        t = self.cur
        if t.text in PREFIX_OPS:
            start_idx = self.pos
            self.advance()
            operand = self.parse_expr(PREFIX_BP)
            children = []
            if operand.kind != "ErrorExpr":
                children.append(operand)
            else:
                self.emit(f"一元运算符 {t.text!r} 缺少操作数", self.cur)
            return self.mk("Unary", start_idx, self.pos,
                           text=t.text, children=children)
        return self.parse_postfix()

    def parse_postfix(self) -> Node:
        atom = self.parse_atom()
        while self.peek_text() == "(":
            call_start = atom.tok_start
            self.advance()  # (
            args, broken = self.parse_arg_list()
            if self.peek_text() == ")":
                self.advance()
            else:
                self.emit("未闭合的调用，缺少 ')'", self.cur)
            # 丢弃因中断产生的错误实参，避免把垃圾结构挂进调用
            if broken:
                args = []
            arglist = self.wrap("ArgList", args)
            atom = self.mk("Call", call_start, self.pos,
                           children=[atom, arglist])
        return atom

    def parse_arg_list(self) -> tuple[list[Node], bool]:
        args: list[Node] = []
        while True:
            x = self.cur
            if x.text == ")" or x.kind == "eof":
                return args, False
            if x.text in (";", "}"):
                self.emit("实参列表解析中断，缺少 ')'", x)
                return args, True
            if _starts_expr(x):
                arg = self.parse_expr(0)
                if arg.kind != "ErrorExpr":
                    args.append(arg)
                if self.peek_text() == ",":
                    self.advance()
                    continue
                if self.peek_text() == ")":
                    return args, False
                self.emit("实参列表中缺少 ',' 或 ')'", self.cur)
                self.skip_to_header_end()
                return args, True
            self.emit("缺少实参表达式", self.cur)
            return args, True

    def parse_atom(self) -> Node:
        """原子表达式。无法开始时返回零宽度 ErrorExpr 节点（而非 None），

        保证“缺少表达式”等错误挂在真实节点上，可随增量复用迁移。"""
        t = self.cur
        start_idx = self.pos
        if t.kind == "number":
            node = self.leaf("Number", self.pos)
            self.advance()
            return node
        if t.kind == "string":
            node = self.leaf("String", self.pos,
                             text=self._string_value(t.text))
            self.advance()
            return node
        if t.kind == "ident":
            node = self.leaf("Ident", self.pos)
            self.advance()
            return node
        if t.text in ("true", "false", "null"):
            node = self.leaf("Literal", self.pos)
            self.advance()
            return node
        if t.text == "(":
            return self.parse_group()
        if t.text == "[":
            return self.parse_array()
        self.emit("缺少表达式", self.cur)
        return self.mk("ErrorExpr", start_idx, start_idx)

    @staticmethod
    def _string_value(raw: str) -> str:
        """去掉字符串外层引号并还原简单转义。"""
        quote = raw[0]
        body = raw[1:-1] if len(raw) > 1 and raw[-1] == quote else raw[1:]
        out: list[str] = []
        i = 0
        escapes = {"n": "\n", "t": "\t", '"': '"', "'": "'", "\\": "\\"}
        while i < len(body):
            c = body[i]
            if c == "\\" and i + 1 < len(body):
                out.append(escapes.get(body[i + 1], body[i + 1]))
                i += 2
            else:
                out.append(c)
                i += 1
        return "".join(out)

    def parse_group(self) -> Node:
        start_idx = self.pos
        self.advance()  # (
        children: list[Node] = []
        if self.cur.text == ")":
            self.emit("括号内缺少表达式", self.cur)
            self.advance()
        else:
            inner = self.parse_expr(0)
            if inner.kind != "ErrorExpr":
                children.append(inner)
            if self.peek_text() == ")":
                self.advance()
            else:
                self.emit("未闭合的括号，缺少 ')'", self.cur)
        return self.mk("Group", start_idx, self.pos, children=children)

    def parse_array(self) -> Node:
        start_idx = self.pos
        self.advance()  # [
        elems: list[Node] = []
        while self.cur.kind != "eof" and self.peek_text() != "]":
            if self.peek_text() in (";", "}", ")"):
                self.emit("数组字面量解析中断，缺少 ']'", self.cur)
                break
            if _starts_expr(self.cur):
                el = self.parse_expr(0)
                if el.kind != "ErrorExpr":
                    elems.append(el)
                if self.peek_text() == ",":
                    self.advance()
                    continue
                if self.peek_text() == "]":
                    break
                self.emit("数组中缺少 ',' 或 ']'", self.cur)
                self.skip_to_header_end()
                break
            self.emit("缺少数组元素表达式", self.cur)
            break
        if self.peek_text() == "]":
            self.advance()
        else:
            self.emit("未闭合的数组，缺少 ']'", self.cur)
        return self.mk("Array", start_idx, self.pos, children=elems)

    # ---------- 小工具 ----------

    def leaf(self, kind: str, tok_idx: int, *, text=None) -> Node:
        t = self.tokens[tok_idx]
        return self.mk(kind, tok_idx, tok_idx + 1,
                       text=text if text is not None else t.text)

    def wrap(self, kind: str, children: list[Node]) -> Node:
        """构造包裹节点（ParamList/ArgList/Else）：范围为子节点并集。

        空列表为零宽度节点，字符位置锚定在“前一个令牌末尾”
        （ParamList/ArgList 的前一个令牌总是开括号）；该前导令牌下标
        记录在 tok_anchor，增量克隆时据此映射到新流。
        """
        if not children:
            anchor = max(self.pos - 1, 0)
            node = self.mk(kind, anchor + 1, anchor + 1)
            node.start = node.end = self.tokens[anchor].end
            node.tok_anchor = anchor
            return node
        start = min(c.tok_start for c in children)
        end = max(c.tok_end for c in children)
        return self.mk(kind, start, end, children=children)


def j_bad(t: Token) -> bool:
    return t.is_lex_error
