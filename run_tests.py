#!/usr/bin/env python3
"""运行全部自动化测试（不依赖 pytest，标准库即可）。

用法：python run_tests.py
退出码：0 全部通过；1 有用例失败（失败项会打印在末尾汇总）。
"""

from __future__ import annotations

import sys
import traceback

from tests import test_api, test_bounds, test_enumeration, test_timeout

TEST_MODULES = [
    test_enumeration,
    test_bounds,
    test_timeout,
    test_api,
]


def main() -> int:
    passed = 0
    failures: list[tuple[str, str]] = []

    for module in TEST_MODULES:
        for name in sorted(dir(module)):
            if not name.startswith("test_"):
                continue
            fn = getattr(module, name)
            if not callable(fn):
                continue
            try:
                fn()
                passed += 1
                print(f"[PASS] {module.__name__}.{name}\n")
            except Exception:  # noqa: BLE001 - 测试运行器需捕获一切
                tb = traceback.format_exc()
                failures.append((f"{module.__name__}.{name}", tb))
                print(f"[FAIL] {module.__name__}.{name}\n{tb}\n")

    print("=" * 70)
    print(f"通过 {passed} 个，失败 {len(failures)} 个")
    if failures:
        print("\n未通过项：")
        for name, _ in failures:
            print(f"  - {name}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
