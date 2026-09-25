"""命令行入口：词法/语法、IR 打印、SSA、回退、解释执行、全流程对照。"""

from __future__ import annotations

import argparse
import json
import sys

from .errors import ToolchainError
from .interpreter import interpret
from .ir import dump_function
from .ir_builder import build_ir
from .parser import parse
from .phi_elimination import eliminate_phis
from .pipeline import ast_to_dict, compile_pipeline
from .ssa_construction import construct_ssa
from .ssa_validate import validate_ssa


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ssa_tool",
        description="小语言工具链：IR 构建、SSA 构造与回退、解释执行（纯后端）")
    ap.add_argument("file", help="ToyLang 源文件（.toy）")
    ap.add_argument("action",
                    choices=("parse", "raw", "ssa", "exec",
                             "run", "pipeline", "check"),
                    help="parse=打印AST(JSON) raw=原始IR ssa=SSA IR "
                         "exec=φ消除后IR run=三种形态对照执行 "
                         "pipeline=全部结果(JSON) check=仅SSA校验")
    ap.add_argument("--args", default="",
                    help="传给 main 的整数参数，逗号分隔，如 3,5")
    ap.add_argument("--max-steps", type=int, default=1_000_000)
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ns = ap.parse_args(argv)

    try:
        source = _read(ns.file)
        args = [int(x) for x in ns.args.split(",") if x.strip()]

        if ns.action == "parse":
            program = parse(source, ns.file)
            print(json.dumps(ast_to_dict(program), ensure_ascii=False, indent=2))
            return 0

        program = parse(source, ns.file)
        raw = build_ir(program)

        if ns.action == "raw":
            print(dump_function(raw))
            return 0

        ssa = construct_ssa(raw.copy())
        violations = validate_ssa(ssa, raise_on_error=False)

        if ns.action == "check":
            if violations:
                for v in violations:
                    print(v)
                print(f"SSA 校验未通过：{len(violations)} 项")
                return 1
            print("SSA 校验通过：每个值单一定义，所有使用均被定义支配，φ 入边完整。")
            return 0

        if ns.action == "ssa":
            print(dump_function(ssa))
            if violations:
                for v in violations:
                    print(v, file=sys.stderr)
                return 1
            return 0

        if violations:
            raise ToolchainError("SSA 校验未通过，拒绝继续 φ 消除")
        exec_fn = eliminate_phis(ssa.copy())

        if ns.action == "exec":
            print(dump_function(exec_fn))
            return 0

        if ns.action == "run":
            results = {}
            for flavor, fn in (("raw", raw), ("ssa", ssa), ("exec", exec_fn)):
                r = interpret(fn, args, max_steps=ns.max_steps)
                results[flavor] = r
                print(f"{flavor:>4}: 返回 {r.value}，执行 {r.steps} 步")
            values = {r.value for r in results.values()}
            steps = {r.steps for r in results.values()}
            if len(values) != 1:
                print("对照失败：返回值不一致", file=sys.stderr)
                return 1
            if len(steps) != 1:
                # SSA 少了 load/store 等，步数允许不同；仅提示
                print("（三种形态步数不同属正常：SSA 去除了 load/store）")
            print("三种形态返回值一致 ✓")
            return 0

        if ns.action == "pipeline":
            res = compile_pipeline(source, ns.file, args=args,
                                   max_steps=ns.max_steps)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res["ok"] else 1

    except ToolchainError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"错误: 找不到文件 {exc.filename}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
