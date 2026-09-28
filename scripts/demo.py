#!/usr/bin/env python3
"""本地端到端演示脚本（不依赖网络与外部账号）。

依次演示：

1. 用合成夹具构建最小 DAWG 并持久化到临时 SQLite；
2. 重新加载，做成员判定与前缀计数，并打印判定依据；
3. 展示固定错误类别（无序输入、空词、悬空/环校验）；
4. 与独立参考 Trie 比对接受语言与前缀计数；
5. 打印最小性小例子的状态数。

运行::

    python scripts/demo.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# 允许从仓库根直接运行。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.dawg import build_dawg
from app.core.trie import ReferenceTrie
from app.corpus.errors import EmptyWordError, UnorderedCorpusError
from app.corpus.fixtures import FIXTURE_SHARED_SUFFIX, random_corpus
from app.corpus.spec import CorpusSpec
from app.diagnostics import configure_logging, new_run_id
from app.index.repository import SQLiteIndexRepository
from app.index.service import IndexService
from app.index.validator import (
    StoredEdge,
    StoredState,
    validate_references,
)
from app.query.errors import QueryRejectedError
from app.query.service import QueryService


def section(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def main() -> int:
    run_id = configure_logging("logs", "INFO", new_run_id())
    print(f"运行身份 run_id={run_id}")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "demo.sqlite3"
        service = IndexService(SQLiteIndexRepository(db_path))

        section("1) 构建并持久化（夹具 shared_suffix）")
        dawg, report = service.build_from_fixture(
            FIXTURE_SHARED_SUFFIX.name, spec=CorpusSpec()
        )
        for line in report.summary_lines():
            print("  " + line)

        section("2) 重新加载并查询")
        persisted = service.load()
        query = QueryService(persisted.dawg, persisted.metadata)
        for word in ("cat", "cats", "dog", "dogss", "walk", ""):
            try:
                result = query.membership(word)
                print(f"  contains({word!r:8}) = {result.is_member!s:5} | {result.verdict()}")
            except QueryRejectedError as exc:
                print(f"  contains({word!r:8}) -> 拒绝 [{exc.error_code}]: {exc}")
        for prefix in ("cat", "dog", "wa", "z"):
            result = query.prefix_count(prefix)
            print(f"  prefix_count({prefix!r:4}) = {result.count} | {result.verdict()}")

        section("3) 固定错误类别")
        try:
            CorpusSpec().normalize(["banana", "apple"])
        except UnorderedCorpusError as exc:
            print(f"  无序输入 -> [{exc.error_code}] {exc}")
        try:
            CorpusSpec(allow_empty_word=False).normalize([""])
        except EmptyWordError as exc:
            print(f"  空词     -> [{exc.error_code}] {exc}")

        section("4) 持久化引用校验：构造悬空与环")
        good_states = [
            StoredState(0, False, 2),
            StoredState(1, True, 1),
            StoredState(2, True, 1),
        ]
        dangling_edges = [StoredEdge(0, "a", 1), StoredEdge(0, "b", 99)]
        dangling = validate_references(good_states, dangling_edges)
        print("  悬空引用违规类别:", sorted({v.kind for v in dangling}))

        cycle_states = [
            StoredState(0, False, 2),
            StoredState(1, True, 1),
        ]
        # 0 -a-> 1, 1 -b-> 0 构成环
        cycle_edges = [StoredEdge(0, "a", 1), StoredEdge(1, "b", 0)]
        cycles = validate_references(cycle_states, cycle_edges)
        print("  环违规类别:", sorted({v.kind for v in cycles}))

        section("5) 与独立参考 Trie 交叉比对（随机合成语料）")
        fixture = random_corpus(size=300, max_word_length=7, seed=20260927)
        words = fixture.as_list()
        d = build_dawg(words)
        trie = ReferenceTrie(words)
        # 候选域：全部词 + 扰动（增删末尾字符、插入不存在字符）。
        domain = set(words)
        for word in words:
            if word:
                domain.add(word[:-1])
                domain.add(word + "z")
                domain.add(word + word[:1])
        domain.add("")
        lang_mismatch = [w for w in domain if d.contains(w) != trie.contains(w)]
        count_mismatch = [w for w in domain if d.prefix_count(w) != trie.prefix_count(w)]
        print(f"  语料: {fixture.description}")
        print(f"  DAWG 状态数={len(d.states)}  Trie 节点数={trie.node_count()}")
        print(f"  接受语言不一致数: {len(lang_mismatch)}")
        print(f"  前缀计数不一致数: {len(count_mismatch)}")
        assert not lang_mismatch and not count_mismatch, "交叉比对失败"

        section("6) 最小性小例子")
        small = build_dawg(["cat", "cats", "dog", "dogs"])
        # 手工可核对：cat/dog 各自前缀链 + 共享 "s" 终结后缀。
        print(f"  词表 cat/cats/dog/dogs -> 最小状态数 = {len(small.states)}")
        print(f"  合并次数 = {small.stats.merge_count}")

    print("\n演示完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
