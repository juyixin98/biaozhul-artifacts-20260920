"""持久化层：SQLite 存储与加载时的引用校验。

单库可存多个命名自动机。schema：
  automata(name PK, fingerprint, word_count, state_count, format/minidfa 版本)
  states(automaton, id, is_final)            主键 (automaton, id)
  transitions(automaton, src, symbol, dst)   主键 (automaton, src, symbol) 保证确定性

加载时强制引用校验（契约 4），任一不满足即拒绝并给出具体类别：
  - 转移的 src/dst 必须存在于 states（悬空引用 → DanglingReferenceError）
  - 初始状态必须存在（→ DanglingReferenceError）
  - 状态图必须无环（→ CycleDetectedError）
  - 所有状态必须从初始状态可达（悬空状态 → UnreachableStateError）
  - 符号必须为单字符（→ InvalidSymbolError，由 MinimalDFA 构造复核）
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from . import __version__
from .automaton import START_STATE, MinimalDFA
from .errors import (
    AutomatonNotFoundError,
    CycleDetectedError,
    DanglingReferenceError,
    UnreachableStateError,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS automata (
    name            TEXT PRIMARY KEY,
    fingerprint     TEXT NOT NULL,
    word_count      INTEGER NOT NULL,
    state_count     INTEGER NOT NULL,
    start_state     INTEGER NOT NULL,
    format_version  TEXT NOT NULL,
    minidfa_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS states (
    automaton TEXT NOT NULL,
    id        INTEGER NOT NULL,
    is_final  INTEGER NOT NULL CHECK (is_final IN (0, 1)),
    PRIMARY KEY (automaton, id)
);
CREATE TABLE IF NOT EXISTS transitions (
    automaton TEXT NOT NULL,
    src       INTEGER NOT NULL,
    symbol    TEXT NOT NULL,
    dst       INTEGER NOT NULL,
    PRIMARY KEY (automaton, src, symbol)
);
"""


class AutomatonStore:
    """命名自动机的 SQLite 仓库。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        # FastAPI 在线程池中处理请求；连接跨线程共享，用锁串行化访问。
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "AutomatonStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def exists(self, name: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM automata WHERE name = ?", (name,)
            ).fetchone()
            return row is not None

    def list_names(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._conn.execute("SELECT name FROM automata ORDER BY name")]

    def save(self, automaton: MinimalDFA, name: str, fingerprint: str) -> None:
        """整体替换写入同名自动机（先删后插，单事务）。"""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM automata WHERE name = ?", (name,))
            self._conn.execute("DELETE FROM states WHERE automaton = ?", (name,))
            self._conn.execute("DELETE FROM transitions WHERE automaton = ?", (name,))
            self._conn.execute(
                "INSERT INTO automata(name, fingerprint, word_count, state_count, "
                "start_state, format_version, minidfa_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    fingerprint,
                    len(automaton),
                    automaton.state_count,
                    START_STATE,
                    "1",
                    __version__,
                ),
            )
            self._conn.executemany(
                "INSERT INTO states(automaton, id, is_final) VALUES (?, ?, ?)",
                [
                    (name, s, 1 if s in automaton.final_states else 0)
                    for s in range(automaton.state_count)
                ],
            )
            self._conn.executemany(
                "INSERT INTO transitions(automaton, src, symbol, dst) VALUES (?, ?, ?, ?)",
                [(name, src, sym, dst) for src, sym, dst in automaton.edge_rows()],
            )

    def load(self, name: str) -> MinimalDFA:
        """加载并执行全部引用校验。校验失败抛出具体类别异常。"""
        with self._lock:
            return self._load_locked(name)

    def _load_locked(self, name: str) -> MinimalDFA:
        row = self._conn.execute(
            "SELECT start_state FROM automata WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise AutomatonNotFoundError(name)
        start = row[0]

        states = {
            r[0]: bool(r[1])
            for r in self._conn.execute(
                "SELECT id, is_final FROM states WHERE automaton = ?", (name,)
            )
        }
        if start not in states:
            raise DanglingReferenceError(
                f"start state {start} does not exist",
                detail={"automaton": name, "start_state": start},
            )

        edges: dict[int, list[tuple[str, int]]] = {}
        for src, symbol, dst in self._conn.execute(
            "SELECT src, symbol, dst FROM transitions WHERE automaton = ?", (name,)
        ):
            if src not in states:
                raise DanglingReferenceError(
                    f"transition source state {src} does not exist",
                    detail={"automaton": name, "src": src, "symbol": symbol},
                )
            if dst not in states:
                raise DanglingReferenceError(
                    f"transition target state {dst} does not exist",
                    detail={"automaton": name, "src": src, "symbol": symbol, "dst": dst},
                )
            edges.setdefault(src, []).append((symbol, dst))

        _check_acyclic(name, states, edges)
        _check_reachable(name, states, edges, start)

        return MinimalDFA(
            final_states=frozenset(s for s, is_final in states.items() if is_final),
            transitions={src: tuple(sorted(es)) for src, es in edges.items()},
            state_count=len(states),
        )

    def delete(self, name: str) -> None:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM automata WHERE name = ?", (name,))
            self._conn.execute("DELETE FROM states WHERE automaton = ?", (name,))
            self._conn.execute("DELETE FROM transitions WHERE automaton = ?", (name,))
        if cur.rowcount == 0:
            raise AutomatonNotFoundError(name)


def _check_acyclic(
    automaton: str, states: dict[int, bool], edges: dict[int, list[tuple[str, int]]]
) -> None:
    """迭代式三色 DFS 判环，避免大自动机上的递归深度问题。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {s: WHITE for s in states}

    for root in states:
        if color[root] != WHITE:
            continue
        # 栈帧：(节点, 下一条待访问边的下标, 当前路径)
        path: list[int] = []
        stack: list[tuple[int, int]] = [(root, 0)]
        color[root] = GRAY
        path.append(root)
        while stack:
            node, idx = stack[-1]
            neighbors = edges.get(node, ())
            if idx < len(neighbors):
                dst = neighbors[idx][1]
                stack[-1] = (node, idx + 1)
                if color[dst] == GRAY:
                    cycle_start = path.index(dst)
                    cycle = path[cycle_start:] + [dst]
                    raise CycleDetectedError(
                        f"cycle detected in {automaton!r}: {' -> '.join(map(str, cycle))}",
                        detail={"automaton": automaton, "cycle": cycle},
                    )
                if color[dst] == WHITE:
                    color[dst] = GRAY
                    path.append(dst)
                    stack.append((dst, 0))
            else:
                color[node] = BLACK
                path.pop()
                stack.pop()


def _check_reachable(
    automaton: str,
    states: dict[int, bool],
    edges: dict[int, list[tuple[str, int]]],
    start: int,
) -> None:
    """从初始状态 DFS，存在不可达状态即拒绝。"""
    seen = {start}
    stack = [start]
    while stack:
        node = stack.pop()
        for _, dst in edges.get(node, []):
            if dst not in seen:
                seen.add(dst)
                stack.append(dst)
    unreachable = sorted(set(states) - seen)
    if unreachable:
        raise UnreachableStateError(
            f"{len(unreachable)} unreachable state(s) in {automaton!r}",
            detail={"automaton": automaton, "unreachable": unreachable[:50]},
        )
