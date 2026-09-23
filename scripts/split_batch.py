"""从已签名 JSONL 中按 event_id 拆出一个或多个批次（用于"迟到事件"验收）。

用法：
  python scripts/split_batch.py all_signed.jsonl out_prefix eid1 eid2 ...
输出 out_prefix_1.jsonl（按命令行给出的顺序，每行一个事件）。
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    src, prefix, *ids = sys.argv[1:]
    by_id: dict[str, dict] = {}
    with open(src, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                ev = json.loads(line)
                by_id[ev["event_id"]] = ev
    out = f"{prefix}_1.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for eid in ids:
            if eid not in by_id:
                raise SystemExit(f"事件 {eid} 不存在于 {src}")
            f.write(json.dumps(by_id[eid], ensure_ascii=False) + "\n")
    print(f"wrote {len(ids)} events -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
