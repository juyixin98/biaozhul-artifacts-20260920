"""把 源码 -> 编译 -> 验证 -> 运行 串起来的便捷流水线。"""

from __future__ import annotations

import io

from .bytecode import Module, disassemble
from .common import ToolError
from .compiler import compile_source
from .interpreter import Interpreter
from .verifier import verify_module


def compile_and_verify(source: str, filename: str = "<input>") -> Module:
    module = compile_source(source, filename)
    verify_module(module)
    return module


def compile_verify_run(source: str, filename: str = "<input>",
                       fuel: int = 100_000, capture: bool = True):
    """返回 (module, result, steps, output_text)。"""
    module = compile_and_verify(source, filename)
    buf = io.StringIO() if capture else None
    interp = Interpreter(module, fuel=fuel, out=buf)
    result = interp.call_main()
    return module, result, interp.steps, (buf.getvalue() if capture else "")


def module_disassembly(module: Module) -> str:
    return "\n\n".join(disassemble(f) for f in module.functions)
