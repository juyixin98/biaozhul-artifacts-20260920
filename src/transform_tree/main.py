"""命令行 JSON 入口。

用法：
    python -m transform_tree <request.json> [-o result.json]
    cat request.json | python -m transform_tree -

每个查询独立求值：单条查询失败（越界、缺口、未知帧等）只在该条结果中记录错误，
不影响其他查询；只要有一条失败，进程退出码为 1；请求本身无法解析/建树失败退出码为 2。
"""

from __future__ import annotations

import argparse
import json
import sys

from .errors import TransformTreeError
from .json_io import build_tree, load_request, parse_query, serialize_transform
from .tree import TransformTree


def process_request(request: dict) -> dict:
    """建树并执行全部查询，返回可 JSON 序列化的响应字典。"""
    tree, default_max_gap = build_tree(request)
    raw_queries = request.get("queries", [])
    if not isinstance(raw_queries, list):
        from .errors import InvalidRequestError

        raise InvalidRequestError("queries 必须是数组")
    results = []
    all_ok = True
    for i, q in enumerate(raw_queries):
        entry: dict = {"index": i}
        try:
            parsed = parse_query(q, i, default_max_gap)
            entry["query"] = {
                "source": parsed["source"],
                "target": parsed["target"],
                "time": parsed["time"],
            }
            chain = tree.chain_frames(parsed["source"], parsed["target"])
            transform = tree.lookup_transform(
                parsed["source"], parsed["target"], parsed["time"], max_gap=parsed["max_gap"]
            )
            entry["ok"] = True
            entry["chain"] = chain
            entry["transform"] = serialize_transform(transform)
        except TransformTreeError as exc:
            all_ok = False
            entry["ok"] = False
            entry["error"] = {"type": type(exc).__name__, "message": str(exc)}
        results.append(entry)
    return {"ok": all_ok, "frame_count": len(tree.frames), "edge_count": len(tree.edges), "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="坐标变换时间树 JSON 入口")
    parser.add_argument("request", help="请求 JSON 文件路径；- 表示从标准输入读取")
    parser.add_argument("-o", "--output", help="结果输出文件；缺省输出到标准输出")
    args = parser.parse_args(argv)

    try:
        if args.request == "-":
            try:
                request = json.load(sys.stdin)
            except json.JSONDecodeError as exc:
                raise _wrap_json_error(exc) from exc
        else:
            request = load_request(args.request)
        response = process_request(request)
    except TransformTreeError as exc:
        response = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
        text = json.dumps(response, ensure_ascii=False, indent=2)
        _emit(text, args.output)
        return 2

    text = json.dumps(response, ensure_ascii=False, indent=2)
    _emit(text, args.output)
    return 0 if response["ok"] else 1


def _wrap_json_error(exc: json.JSONDecodeError) -> TransformTreeError:
    from .errors import InvalidRequestError

    return InvalidRequestError(f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}")


def _emit(text: str, output: str | None) -> None:
    if output:
        with open(output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)


if __name__ == "__main__":
    sys.exit(main())
