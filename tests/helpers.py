"""测试辅助：从源码字符串直接跑完整管道。"""
from __future__ import annotations

from tinyinfer.pipeline import analyze, parse_source
from tinyinfer.inference import infer_program


def infer_type(source: str, **kw) -> str:
    program = parse_source(source)
    return infer_program(program, source=source, **kw).type_str()


def analyze_ok(source: str, **kw) -> dict:
    payload = analyze(source, **kw)
    assert payload["ok"], payload
    return payload


def analyze_err(source: str, **kw) -> dict:
    payload = analyze(source, **kw)
    assert not payload["ok"], payload
    return payload["error"]
