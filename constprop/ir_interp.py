"""SSA IR 解释器（优化前与优化后共用同一执行器）。

执行模型：**急切求值 + 命令式值环境**。

- SSA 的“单赋值”是静态性质；运行期进入循环块时，循环体指令与 φ
  会在每次迭代重新执行、重新绑定同名值。env 是名字 -> 整数的
  命令式映射，进入基本块时先按“前驱标签”求值块首 φ，再顺序执行
  其余指令——这天然正确处理循环携带依赖。
- 未定义变量经 SSA 构造得到 :data:`~constprop.model.UNDEF`。为了让
  “未使用的 φ / copy 携带 undef”不凭空报错（例如变量只在不可达
  分支赋值、join 后从未使用），undef 在纯定义链上以**单元标记**
  传播，只有在真正的观察点（``print``、``br``、算术运算的操作数
  读取）才抛 ``undefined-variable``，错误位置即该读取指令的位置。
- ``/``/``%`` 除零在操作数求值时抛 ``division-by-zero``；
  带步进上限，防止异常输入死循环。
"""

from __future__ import annotations

from .interp import RunResult
from .model import BINOPS, UNOPS, CFG, UNDEF, Inst
from .runtime import apply_binop, apply_unop, truthy
from .source import (
    STACK_LIMIT,
    UNDEFINED_VAR,
    RuntimeErr,
    SourceText,
    Span,
)

#: 运行期“未定义单元”：可沿纯 copy/φ 链传播，在观察点引爆
_UNDEF_CELL = object()


class IRInterpreter:
    def __init__(self, cfg: CFG, source: SourceText | None = None,
                 step_limit: int = 2_000_000):
        self.cfg = cfg
        self.src = source
        self.step_limit = step_limit
        self.steps = 0
        # 名字 -> int | _UNDEF_CELL；随块访问被反复重绑定（循环）
        self.env: dict[str, object] = {}
        self.outputs: list[int] = []

    def tick(self, span: Span | None) -> None:
        self.steps += 1
        if self.steps > self.step_limit:
            raise RuntimeErr(
                f"step limit {self.step_limit} exceeded (possible infinite loop)",
                STACK_LIMIT, span, self.src,
            )

    def err(self, message: str, code: str, span: Span | None) -> RuntimeErr:
        return RuntimeErr(message, code, span, self.src)

    # ---------- 操作数求值（观察点：undef 在此引爆） ----------

    def read(self, op, span: Span | None) -> int:
        if isinstance(op, int):
            return op
        if op == UNDEF:
            raise self.err("read of undefined variable", UNDEFINED_VAR, span)
        v = self.env.get(op, _UNDEF_CELL)
        if v is _UNDEF_CELL:
            raise self.err("read of undefined variable", UNDEFINED_VAR, span)
        return v  # type: ignore[return-value]

    def resolve(self, op) -> object:
        """不求观察点语义地解析：返回 int 或 undef 单元（φ/copy 传播用）。"""
        if isinstance(op, int):
            return op
        if op == UNDEF:
            return _UNDEF_CELL
        return self.env.get(op, _UNDEF_CELL)

    # ---------- 主循环 ----------

    def run(self) -> RunResult:
        result = RunResult()
        prev_label: str | None = None
        label = self.cfg.entry
        try:
            while True:
                block = self.cfg.block(label)

                # 块首 φ：按前驱选入边；undef 以单元形式绑定（不立即报错）
                for inst in block.insts:
                    if not inst.is_phi:
                        break
                    self.tick(inst.span)
                    arg = self._select_phi_arg(inst, prev_label)
                    assert inst.target is not None
                    self.env[inst.target] = self.resolve(arg)

                next_label: str | None = None
                terminated = False
                for inst in block.insts:
                    if inst.is_phi:
                        continue
                    self.tick(inst.span)
                    if inst.is_terminator:
                        next_label = self._exec(inst)
                        terminated = True
                        break
                    self._exec(inst)

                if not terminated:
                    break  # 块末无终结符：程序结束
                if next_label is None:
                    break  # exit
                prev_label, label = label, next_label
        except RuntimeErr as e:
            result.error_code = e.code
            result.error_message = e.message
            if e.span is not None:
                result.location = (e.span.start_line, e.span.start_col)
        result.steps = self.steps
        result.output = list(self.outputs)
        return result

    def _select_phi_arg(self, inst: Inst, prev_label: str | None):
        if prev_label is None and len(inst.phi_args) == 1:
            return inst.phi_args[0].value
        for arg in inst.phi_args:
            if arg.block == prev_label:
                return arg.value
        raise self.err(
            f"phi has no argument for predecessor {prev_label!r}",
            "internal-error", inst.span,
        )

    # ---------- 指令 ----------

    def _exec(self, inst: Inst):
        """执行非 φ 指令；终结符返回目标块标签（exit 返回 None）。"""
        if inst.op == "lit":
            assert inst.target is not None
            self.env[inst.target] = int(inst.operands[0])  # type: ignore[arg-type]
            return None
        if inst.op == "copy":
            assert inst.target is not None
            # undef 单元可无害穿过未被读取的 copy
            self.env[inst.target] = self.resolve(inst.operands[0])
            return None
        # 注意 '-' 同时是一元负号与二元减法：二元分支要求恰好两个操作数，
        # 必须在一元分支之前判定。
        if inst.op in BINOPS and len(inst.operands) == 2:
            assert inst.target is not None
            # undef 沿纯算术惰性传播：任一操作数为 undef => 结果 undef，
            # 不进行运算（因而不触发除零）；仅观察点（print/br）引爆。
            a, b = self.resolve(inst.operands[0]), self.resolve(
                inst.operands[1])
            if a is _UNDEF_CELL or b is _UNDEF_CELL:
                self.env[inst.target] = _UNDEF_CELL
            else:
                self.env[inst.target] = apply_binop(
                    inst.op, a, b, inst.span, self.src)  # type: ignore[arg-type]
            return
        if inst.op in UNOPS:
            assert inst.target is not None
            a = self.resolve(inst.operands[0])
            self.env[inst.target] = (_UNDEF_CELL if a is _UNDEF_CELL
                                     else apply_unop(inst.op, a))  # type: ignore[arg-type]
            return
        if inst.op == "print":
            self.outputs.append(self.read(inst.operands[0], inst.span))
            return None
        if inst.op == "br":
            c = self.read(inst.operands[0], inst.span)
            return inst.blocks[0] if truthy(c) else inst.blocks[1]
        if inst.op == "jmp":
            return inst.blocks[0]
        if inst.op == "exit":
            return None
        raise self.err(f"unknown IR op {inst.op!r}",  # pragma: no cover
                       "internal-error", inst.span)


def run_ir(cfg: CFG, source: SourceText | None = None,
           step_limit: int = 2_000_000) -> RunResult:
    return IRInterpreter(cfg, source, step_limit=step_limit).run()
