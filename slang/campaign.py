"""变异测试驱动器（mutant campaign）。

对一份“验证通过”的模块施加全部（或有针对性的）单字节变异，
逐条重新解码 -> 验证 -> （仅验证通过者）带燃料解释执行，
按结果分类统计：

- decode_rejected   信封/函数字节解码即失败
- verify_rejected   验证器拒绝（记录错误码与最短错误路径）
- runtime_error     通过验证但运行期报错（除零/燃料/递归深度）
- ran_clean         带燃料跑完 main 未报错
- invariant_broken  ★验证通过却发生栈下溢等不变量破坏（任何一条都意味着
                    验收关键性质“通过验证后不应栈下溢”被违反）

验收要求的覆盖（回边 / 异常返回 / 越界跳转）通过内置示例程序与
针对性变异天然覆盖；这里还会额外报告错误码分布与若干代表样本。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .bytecode import decode_module
from .errors import InvariantBroken, RuntimeErr
from .interpreter import Interpreter
from .mutator import (Mutation, apply_mutation, exhaustive_mutations,
                      targeted_mutations)
from .verifier import verify_module


@dataclass
class MutantOutcome:
    mutation: Mutation
    category: str
    error_code: str | None = None
    message: str = ""
    path: list = field(default_factory=list)
    steps: int = 0


def _classify(module, m: Mutation, fuel: int) -> MutantOutcome:
    raw = apply_mutation(module, m)
    try:
        mut_mod = decode_module(raw)
    except Exception as e:
        return MutantOutcome(m, "decode_rejected", "DECODE_ERROR", str(e))

    errors = []
    try:
        errors = verify_module(mut_mod)
    except Exception as e:  # 验证器自身崩溃也算“被发现”，但单独标记
        return MutantOutcome(m, "verifier_crashed", "VERIFIER_CRASH", repr(e))

    if errors:
        e0 = errors[0]
        return MutantOutcome(m, "verify_rejected", e0.code, e0.message,
                             [n.__dict__ for n in e0.path])

    # 验证通过：防御性执行，专门观察不变量破坏（尤其栈下溢）
    try:
        interp = Interpreter(mut_mod, fuel=fuel, verify_first=False)
        interp.run_main()
        return MutantOutcome(m, "ran_clean", None, "main 在燃料内正常结束",
                             steps=interp.steps)
    except InvariantBroken as e:
        return MutantOutcome(m, "invariant_broken", "INVARIANT_BROKEN", str(e))
    except RuntimeErr as e:
        return MutantOutcome(m, "runtime_error", "RUNTIME", str(e),
                             steps=getattr(e, "steps", 0))
    except Exception as e:  # 解释器其它崩溃：记录，不该发生
        return MutantOutcome(m, "interpreter_crashed", "INTERPRETER_CRASH",
                             repr(e))


def run_campaign(module, strategy: str = "targeted",
                 func_index: int | None = None,
                 fuel: int = 20_000,
                 progress_every: int = 0) -> dict:
    if strategy == "exhaustive":
        mutations = exhaustive_mutations(module, func_index=func_index)
    else:
        mutations = targeted_mutations(module, func_index=func_index)

    outcomes: list[MutantOutcome] = []
    for k, m in enumerate(mutations):
        outcomes.append(_classify(module, m, fuel))
        if progress_every and (k + 1) % progress_every == 0:
            print(f"  ... {k + 1}/{len(mutations)}")

    by_cat = Counter(o.category for o in outcomes)
    by_code = Counter(o.error_code for o in outcomes
                      if o.category == "verify_rejected")
    examples: dict[str, MutantOutcome] = {}
    for o in outcomes:
        if o.category not in examples:
            examples[o.category] = o

    return {
        "strategy": strategy,
        "total": len(outcomes),
        "by_category": dict(by_cat),
        "verify_error_codes": dict(by_code),
        "examples": {
            cat: {
                "mutation": o.mutation.label,
                "error_code": o.error_code,
                "message": o.message,
                "path": o.path,
                "steps": o.steps,
            }
            for cat, o in examples.items()
        },
        "outcomes": outcomes,
    }
