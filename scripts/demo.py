#!/usr/bin/env python3
"""本地演示：构建 → 持久化 → 查询 → 错误语义，全程打印判定依据。

用法: python3 scripts/demo.py [db_path]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minidfa import __version__
from minidfa.config import Settings, configure_logging
from minidfa.corpus import InputMode
from minidfa.errors import MinDfaError
from minidfa.service import DfaService

WORDS = ["ape", "apple", "applet", "banana", "band", "bandit"]


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "./demo.db"
    configure_logging("INFO")
    print(f"=== minidfa {__version__} demo, db={db_path} ===")

    service = DfaService(Settings(db_path=db_path))
    try:
        report = service.build("fruits", WORDS)
        print(f"[build] run={report.run_id} fingerprint={report.fingerprint} "
              f"words={report.word_count} states={report.state_count} edges={report.edge_count}")

        for word in ["apple", "app", "banana", ""]:
            print(f"[contains] {word!r:10} -> {service.contains('fruits', word)}")
        for prefix in ["", "ap", "app", "ban", "band", "zzz"]:
            print(f"[prefix_count] {prefix!r:10} -> {service.prefix_count('fruits', prefix)}")

        print("[error semantics] strict mode, unsorted input ['b','a']:")
        try:
            service.build("bad", ["b", "a"])
        except MinDfaError as exc:
            print(f"  rejected: category={exc.category} message={exc.message}")

        report2 = service.build("normalized", ["b", "a", "b"], InputMode.NORMALIZE)
        print(f"[normalize] sorted+deduped -> words={report2.word_count}")

        print(f"[stats] {service.stats('fruits')}")
    finally:
        service.close()
    print("=== demo done ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
