"""不依赖服务的纯本地抽取演示：python -m app.demo_offline

对一段真实技术新闻文本跑内置规则，打印四类实体、原文位置、命中规则与规范名，
并演示别名归一化不丢原始提及。结果完全由文本内容决定，不是固定夹具。
"""
from __future__ import annotations

import json
import pathlib

from .rules_engine import compile_pack_json

SAMPLE = """2024年3月15日，北京大学的李明教授在接受采访时说，团队与阿里巴巴集团
合作，把一个基于 Python 3.12.1 和 SQLAlchemy 的知识抽取原型迁移到了 PostgreSQL 16.2。
王芳指出，旧系统使用 SQLite 与 Flask 构建，部署在 Docker 中。
同日，Xerox PARC 的 Alan Kay 回忆，Smalltalk 的面向对象编程深刻影响了后来的图形界面。
Linus Torvalds 曾在 Sept 2nd, 2024 的邮件列表里讨论过内核调度；华为技术有限公司
也在 2024年3月 发布了新的编译器分支 openAi_compat。
"""


def main() -> None:
    pack_path = pathlib.Path(__file__).resolve().parent.parent / "rules" / "builtin_v1.json"
    pack = compile_pack_json(pack_path.read_text(encoding="utf-8"))
    print(f"规则包版本: {pack.version}")
    print(f"规则数: {len(pack.rules)}\n")

    # 直接调用引擎（不经过任何固定结果）
    from .rules_engine import extract

    mentions = extract(SAMPLE, pack)
    print(f"文本长度: {len(SAMPLE)} 个 Unicode 码点，命中 {len(mentions)} 个提及\n")

    by_type: dict[str, list] = {}
    for m in mentions:
        by_type.setdefault(m.entity_type, []).append(m)

    labels = {"person": "人名", "org": "组织", "tech": "技术名", "date": "日期"}
    for etype in ("person", "org", "tech", "date"):
        print(f"== {labels[etype]} ({etype}) ==")
        for m in by_type.get(etype, []):
            snippet = SAMPLE[m.start:m.end]
            assert snippet == m.matched_text
            print(
                f"  [{m.start:>3}:{m.end:<3}] {m.matched_text!r:28} "
                f"canonical={m.canonical!r:24} alias={m.alias!r:20} rule={m.rule_id}"
            )
        print()

    # 证明不同输入产出不同结果（而非固定返回）
    other = "张伟说，他明天要和 IBM 的团队开会，时间定在 2025-01-08。"
    other_mentions = extract(other, pack)
    print("另一段文本的抽取（验证非固定结果）:")
    print(json.dumps(
        [
            {
                "type": m.entity_type,
                "text": m.matched_text,
                "canonical": m.canonical,
                "span": [m.start, m.end],
                "rule": m.rule_id,
            }
            for m in other_mentions
        ],
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
