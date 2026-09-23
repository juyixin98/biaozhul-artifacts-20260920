"""AST 树遍历参考解释器。

这是 L0 语义的“金标准”：优化后的 IR 执行结果必须与它完全一致
（输出逐行相同；若出错，错误分类 code 与出错行一致）。

作用域
------
**函数级（flat）作用域。** L0 没有声明关键字，``{ }`` 只用于把多条
语句组成一条语句（if/while 的体），**不**引入新的变量作用域。

未定义变量的语义（重要）
------------------------
读取一个从未赋值的名字得到一个**未定义单元（undef）**。undef 会在
纯运算（算术 / 比较 / 一元 / 赋值传递）中**惰性传播**，只有在它抵达
**观察点**——``print`` 的参数或 ``if``/``while`` 的条件——时才抛
``undefined-variable``。

这条规则使得“读取未定义变量但结果从未被观察”的死表达式不产生任何
可观察错误，因此优化器删除这种死代码（DCE）、以及用 undef 乐观合流
（SCCP 标准做法）都保持语义。这与主流编译器对 poison/undef 的处理
一致。错误位置取观察点（print / 条件所在行）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ast as A
from .runtime import apply_binop, apply_unop, truthy
from .source import STACK_LIMIT, UNDEFINED_VAR, RuntimeErr, SourceText

#: 未定义单元：沿纯运算传播，在观察点引爆
_UNDEF = object()
Value = "int | object"  # 运行期值：int 或 _UNDEF


@dataclass
class RunResult:
    output: list[int] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    location: tuple[int, int] | None = None  # (line, col)
    steps: int = 0

    @property
    def ok(self) -> bool:
        return self.error_code is None

    @property
    def output_text(self) -> str:
        return "\n".join(str(x) for x in self.output)

    def signature(self) -> dict:
        """优化前后等价性比较只看可观察行为。

        - ``output``：逐行整数输出；
        - ``error_code``：错误分类（division-by-zero / undefined-variable …）；
        - 出错的**行号**。

        不比较列号：AST 直接在 ``Var`` 节点（变量 token 处）报错，而
        IR 把一个表达式拆成多条指令，读取点指令的 span 可能起于表达式
        左侧（如二元运算起点），列号会不同——这不改变可观察语义。
        """
        return {
            "output": self.output,
            "error_code": self.error_code,
            "error_line": self.location[0] if self.location else None,
        }

    def to_dict(self) -> dict:
        return {
            "output": self.output,
            "output_text": self.output_text,
            "ok": self.ok,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "location": list(self.location) if self.location else None,
            "steps": self.steps,
        }


class Interpreter:
    def __init__(self, source: SourceText, step_limit: int = 2_000_000):
        self.src = source
        self.step_limit = step_limit
        self.steps = 0
        self.outputs: list[int] = []

    def tick(self) -> None:
        self.steps += 1
        if self.steps > self.step_limit:
            raise RuntimeErr(
                f"step limit {self.step_limit} exceeded (possible infinite loop)",
                STACK_LIMIT, None, self.src,
            )

    def run(self, program: A.Program) -> RunResult:
        result = RunResult()
        env: dict[str, object] = {}   # 函数级 flat 变量环境
        try:
            for stmt in program.body:
                self.exec_stmt(stmt, env)
        except RuntimeErr as e:
            result.error_code = e.code
            result.error_message = e.message
            if e.span is not None:
                result.location = (e.span.start_line, e.span.start_col)
        result.steps = self.steps
        result.output = list(self.outputs)
        return result

    def _observe(self, v: object, span) -> int:
        """观察点（print 参数 / 分支条件）：undef 在此引爆。"""
        if v is _UNDEF:
            raise RuntimeErr("read of undefined variable",
                             UNDEFINED_VAR, span, self.src)
        return v  # type: ignore[return-value]

    # ---------- 语句 ----------

    def exec_stmt(self, stmt: A.Stmt, env: dict[str, object]) -> None:
        self.tick()
        if isinstance(stmt, A.Block):
            for s in stmt.body:
                self.exec_stmt(s, env)
            return
        if isinstance(stmt, A.Assign):
            env[stmt.name] = self.eval(stmt.value, env)
            return
        if isinstance(stmt, A.Print):
            self.outputs.append(self._observe(self.eval(stmt.value, env),
                                              stmt.value.span))
            return
        if isinstance(stmt, A.If):
            c = self._observe(self.eval(stmt.cond, env), stmt.cond.span)
            if truthy(c):
                self.exec_stmt(stmt.then, env)
            elif stmt.otherwise is not None:
                self.exec_stmt(stmt.otherwise, env)
            return
        if isinstance(stmt, A.While):
            while truthy(self._observe(self.eval(stmt.cond, env),
                                       stmt.cond.span)):
                self.exec_stmt(stmt.body, env)
            return
        raise AssertionError(type(stmt))  # pragma: no cover

    # ---------- 表达式（undef 惰性传播，除零仍立即抛） ----------

    def eval(self, expr: A.Expr, env: dict[str, object]) -> object:
        self.tick()
        if isinstance(expr, A.IntLit):
            return expr.value
        if isinstance(expr, A.BoolLit):
            return int(expr.value)
        if isinstance(expr, A.Var):
            return env.get(expr.name, _UNDEF)
        if isinstance(expr, A.Unary):
            a = self.eval(expr.operand, env)
            return _UNDEF if a is _UNDEF else apply_unop(expr.op, a)
        if isinstance(expr, A.Binary):
            a = self.eval(expr.left, env)
            b = self.eval(expr.right, env)
            # 任一操作数 undef：结果 undef（不计算，故不触发除零）
            if a is _UNDEF or b is _UNDEF:
                return _UNDEF
            return apply_binop(expr.op, a, b, expr.span, self.src)
        if isinstance(expr, A.Logical):
            # 左值决定短路走向 = 控制流决策，是观察点（undef 左值报错）。
            a = self._observe(self.eval(expr.left, env), expr.left.span)
            if expr.op == "&&":
                if not truthy(a):
                    return 0
                v = self.eval(expr.right, env)
                # 右值 undef：结果惰性为 undef（不报错）；否则规范化为 0/1
                return _UNDEF if v is _UNDEF else int(truthy(v))
            # ||
            if truthy(a):
                return 1
            v = self.eval(expr.right, env)
            return _UNDEF if v is _UNDEF else int(truthy(v))
        raise AssertionError(type(expr))  # pragma: no cover


def run_ast(program: A.Program, source: SourceText,
            step_limit: int = 2_000_000) -> RunResult:
    return Interpreter(source, step_limit=step_limit).run(program)
