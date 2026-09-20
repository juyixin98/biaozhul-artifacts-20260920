#!/usr/bin/env python3
"""写入演示模板（草稿 v1），不自动发布，便于手工体验发布门禁。

用法：
    python -m scripts.seed_demo            # 创建模板 leave-demo
    python -m scripts.seed_demo --publish  # 创建并发布 v1
"""
from __future__ import annotations

import argparse
import json
import pathlib

from app.db import SessionLocal
from app import engine


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", default="leave-demo")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()

    definition = json.loads(
        (pathlib.Path(__file__).parent.parent / "demo" / "leave-demo.json").read_text(
            encoding="utf-8"
        )
    )

    with SessionLocal() as db:
        tpl = engine.create_template(db, args.key, "请假/报销演示流程", definition)
        print(f"模板已创建: key={tpl.key} id={tpl.id}（v1 草稿）")
        if args.publish:
            version = engine.publish_version(db, args.key, 1)
            print(f"v1 已发布: version={version.version}")


if __name__ == "__main__":
    main()
