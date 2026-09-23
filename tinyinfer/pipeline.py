"""解析 / 推导 / 求值 的统一管道与 JSON 序列化。

:mod:`tinyinfer.server` 与 :mod:`tinyinfer.cli` 都建立在本模块之上，
保证两种入口行为一致。
"""
from __future__ import annotations

from typing import Any

from . import ast
from .errors import TinyError
from .eval import Evaluator, value_to_str
from .inference import InferResult, infer_program, lower_program
from .lexer import Lexer
from .parser import parse
from .types import AnnError


def parse_source(source: str, filename: str = "<input>") -> ast.Program:
    tokens = Lexer(source, filename=filename).tokenize()
    return parse(tokens)


def error_payload(err: Exception, source: str | None = None) -> dict[str, Any]:
    """把错误转成统一 JSON 结构。"""
    if isinstance(err, TinyError):
        return {
            "ok": False,
            "error": {
                "kind": type(err).__name__,
                "message": err.message,
                "span": err.span.to_dict() if err.span is not None else None,
                "snippet": err.span.snippet(source) if (
                    source is not None and err.span is not None
                ) else "",
            },
        }
    if isinstance(err, AnnError):
        return {"ok": False, "error": {"kind": "AnnotationError",
                                       "message": str(err), "span": None,
                                       "snippet": ""}}
    return {"ok": False, "error": {"kind": type(err).__name__,
                                   "message": str(err), "span": None,
                                   "snippet": ""}}


def result_payload(
    result: InferResult,
    *,
    evaluated: str | None = None,
    eval_error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "type": result.type_str(),
        "bindings": [
            {"name": name, "scheme": scheme}
            for name, scheme in result.binding_schemes()
        ],
        "value": evaluated,
        "eval_error": eval_error,
        "trace": [ev.to_dict() for ev in result.trace],
    }


def analyze(
    source: str,
    *,
    value_restriction: bool = True,
    annotate: bool = True,
    evaluate: bool = True,
    filename: str = "<input>",
) -> dict[str, Any]:
    """完整管道：词法 → 语法 → 类型推导 →（可选）求值。

    返回可直接 ``json.dumps`` 的字典。类型检查失败时不会求值；
    传入 ``evaluate=True`` 且检查通过时运行程序；求值失败（典型场景：
    naive 多态下的引用不健全反例）也照常返回推导结果，错误放在
    ``eval_error`` 中。
    """
    try:
        program = parse_source(source, filename=filename)
        result = infer_program(
            program,
            value_restriction=value_restriction,
            annotate=annotate,
            source=source,
        )
    except Exception as err:  # noqa: BLE001 - 统一转 JSON 错误
        return error_payload(err, source)

    evaluated: str | None = None
    eval_error: dict[str, Any] | None = None
    if evaluate:
        expr = lower_program(program)
        ev = Evaluator()
        try:
            assert expr is not None
            evaluated = value_to_str(ev.eval(expr))
        except Exception as err:  # noqa: BLE001
            eval_error = error_payload(err, source)["error"]

    return result_payload(result, evaluated=evaluated, eval_error=eval_error)


__all__ = ["parse_source", "analyze", "error_payload", "result_payload"]
