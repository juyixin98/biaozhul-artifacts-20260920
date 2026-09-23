"""生成 examples/requests/ 下的 JSON 请求样例（含已编译二进制）。

用法: python examples/requests/generate.py
这些样例可直接配合 curl 使用（见同目录 README.md）。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from slang.compiler import compile_source  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def write(name: str, obj: dict) -> None:
    with open(os.path.join(HERE, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


SRC = """fn sumto(int n): int {
    var i = 1;
    var acc = 0;
    while (i <= n) {
        acc = acc + i;
        i = i + 1;
    }
    return acc;
}

fn main() {
    print sumto(10);
}
"""


def main() -> None:
    module = compile_source(SRC, module_name="sum_loop")
    binary_hex = module.encode().hex()

    write("01_compile.json", {
        "source": SRC, "name": "sum_loop", "encoding": "hex",
    })
    write("02_verify.json", {
        "module": {"binary": binary_hex, "encoding": "hex"},
    })
    write("03_run.json", {
        "module": {"binary": binary_hex, "encoding": "hex"},
        "fuel": 1000000,
    })
    write("04_mutate.json", {
        "module": {"binary": binary_hex, "encoding": "hex"},
        "strategy": "targeted", "limit": 50,
    })

    # 一个会在编译期报错的请求（缺少分号）
    write("05_compile_error.json", {
        "source": "fn main() {\n    print 1\n}\n",
    })

    print(f"已在 {HERE} 生成 5 个请求样例")


if __name__ == "__main__":
    main()
