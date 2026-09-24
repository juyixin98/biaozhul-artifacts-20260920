"""DH 参数文件加载（YAML）。

独立模块，避免 app.config 与 app.core 包初始化之间的循环导入。
"""

from __future__ import annotations

import re
from pathlib import Path

PARAMS_PATH = Path(__file__).resolve().parent / "robot_params.yaml"


def _coerce_scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1]
    if v == "true":
        return True
    if v == "false":
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _parse_inline_value(val: str):
    val = val.strip()
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        return [_coerce_scalar(x) for x in inner.split(",")] if inner else []
    if val.startswith("{") and val.endswith("}"):
        pairs = re.findall(r"([A-Za-z0-9_]+):\s*([^,}]+)", val[1:-1])
        return {k: _coerce_scalar(x) for k, x in pairs}
    return _coerce_scalar(val)


def _tiny_yaml_load(text: str) -> dict:
    """仅支持本项目 robot_params.yaml 结构（缩进映射 / "- {…}" 列表）的最小解析器。"""
    stack: list[tuple[int, object]] = [(-1, {})]
    lines = [ln.split("#", 1)[0].rstrip() for ln in text.splitlines()]

    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        body = raw.strip()

        while indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if body.startswith("- "):
            assert isinstance(parent, list), raw
            parent.append(_parse_inline_value(body[2:]))
            continue

        m = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", body)
        assert m, f"无法解析 YAML 行: {raw!r}"
        key, val = m.group(1), m.group(2)
        assert isinstance(parent, dict), raw
        if val.strip() == "":
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            child: object = [] if (j < len(lines) and lines[j].lstrip().startswith("- ")) else {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_inline_value(val)

    return stack[0][1]


def load_params(path: str | Path = PARAMS_PATH) -> dict:
    """读取 YAML 参数文件，优先 PyYAML，否则用内置最小解析器。"""
    path = Path(path)
    try:
        import yaml  # type: ignore

        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except ImportError:
        return _tiny_yaml_load(path.read_text(encoding="utf-8"))
