"""持久化引用完整性校验（契约 4）。

校验对象是与存储无关的朴素记录（:class:`StoredState` / :class:`StoredEdge`），
因此同一套规则既能校验从 SQLite 读出的内容，也能在写入前校验内存模型，
还能在测试中手工构造"被损坏"的记录来断言失败类别。

校验类别（见 :mod:`app.index.errors` 常量）：

* ``root_missing``        根状态缺失；
* ``dangling_edge``       转移引用了不存在的状态（悬空）；
* ``unreachable_state``   存在从根不可达的状态；
* ``cycle``               可达子图中存在环；
* ``nondeterministic_transition`` / ``duplicate_symbol``
                           同一源状态同一符号有两条出边；
* ``bad_final_flag``      终结标记非法；
* ``word_count_missing`` / ``stored_word_count_mismatch``
                           词数缺失或与结构重算值不一致。
"""

from dataclasses import dataclass

from app.index.errors import (
    IntegrityViolation,
    VIOLATION_BAD_FINAL_FLAG,
    VIOLATION_COUNT_MISMATCH,
    VIOLATION_COUNT_MISSING,
    VIOLATION_CYCLE,
    VIOLATION_DANGLING_EDGE,
    VIOLATION_DUPLICATE_STATE,
    VIOLATION_DUPLICATE_SYMBOL,
    VIOLATION_NONDETERMINISTIC,
    VIOLATION_ROOT_MISSING,
    VIOLATION_UNREACHABLE_STATE,
)

ROOT_ID = 0


@dataclass(frozen=True)
class StoredState:
    state_id: int
    final: bool
    word_count: int | None


@dataclass(frozen=True)
class StoredEdge:
    source: int
    symbol: str
    target: int


def validate_references(
    states: list[StoredState],
    edges: list[StoredEdge],
    *,
    root_id: int = ROOT_ID,
) -> list[IntegrityViolation]:
    """对持久化记录执行全部引用完整性校验，返回违规列表（空列表=通过）。

    本函数只报告、不抛异常，便于一次性收集所有判定依据；由上层决定是否
    抛出 :class:`IndexIntegrityError`。
    """
    violations: list[IntegrityViolation] = []

    # ---- 状态表 ----------------------------------------------------------
    state_ids: set[int] = set()
    finals: dict[int, bool] = {}
    counts: dict[int, int | None] = {}
    for state in states:
        if state.state_id in state_ids:
            violations.append(
                IntegrityViolation(
                    kind=VIOLATION_DUPLICATE_STATE,
                    detail=f"状态 id {state.state_id} 在状态表中重复出现",
                )
            )
            continue
        if not isinstance(state.final, bool):
            violations.append(
                IntegrityViolation(
                    kind=VIOLATION_BAD_FINAL_FLAG,
                    detail=f"状态 {state.state_id} 的终结标记不是布尔值: {state.final!r}",
                )
            )
        state_ids.add(state.state_id)
        finals[state.state_id] = bool(state.final)
        counts[state.state_id] = state.word_count

    if root_id not in state_ids:
        violations.append(
            IntegrityViolation(
                kind=VIOLATION_ROOT_MISSING,
                detail=f"根状态 id={root_id} 不存在，状态集合: {sorted(state_ids)}",
            )
        )
        # 没有根就无法做可达性/环校验，直接返回。
        return violations

    # ---- 转移表：重复符号与悬空引用 --------------------------------------
    adjacency: dict[int, dict[str, int]] = {}
    for edge in edges:
        if edge.source not in state_ids:
            violations.append(
                IntegrityViolation(
                    kind=VIOLATION_DANGLING_EDGE,
                    detail=f"边 {edge.source} --{edge.symbol!r}--> {edge.target} 的源状态不存在",
                )
            )
            continue
        if edge.target not in state_ids:
            violations.append(
                IntegrityViolation(
                    kind=VIOLATION_DANGLING_EDGE,
                    detail=f"边 {edge.source} --{edge.symbol!r}--> {edge.target} 指向不存在的悬空状态",
                )
            )
            # 仍记录源侧邻接用于后续检查，但不进入可达性目标。
        outgoing = adjacency.setdefault(edge.source, {})
        if edge.symbol in outgoing:
            violations.append(
                IntegrityViolation(
                    kind=VIOLATION_NONDETERMINISTIC,
                    detail=(
                        f"状态 {edge.source} 上符号 {edge.symbol!r} 有两条出边: "
                        f"{outgoing[edge.symbol]} 与 {edge.target}（自动机必须确定）"
                    ),
                )
            )
            violations.append(
                IntegrityViolation(
                    kind=VIOLATION_DUPLICATE_SYMBOL,
                    detail=f"状态 {edge.source} 的转移中符号 {edge.symbol!r} 重复",
                )
            )
        else:
            outgoing[edge.symbol] = edge.target

    # ---- 可达性 + 环检测（白/灰/黑 DFS，仅沿指向存在状态的边）------------
    reachable: set[int] = set()
    on_stack: set[int] = set()
    finished: set[int] = set()
    has_cycle = False

    def dfs(start: int) -> None:
        nonlocal has_cycle
        stack: list[tuple[int, list[str]]] = [(start, sorted(adjacency.get(start, {})))]
        reachable.add(start)
        on_stack.add(start)
        while stack:
            node, pending = stack[-1]
            if not pending:
                on_stack.discard(node)
                finished.add(node)
                stack.pop()
                continue
            symbol = pending.pop()
            target = adjacency[node][symbol]
            if target not in state_ids:
                continue  # 悬空已记录
            if target in on_stack:
                has_cycle = True
                violations.append(
                    IntegrityViolation(
                        kind=VIOLATION_CYCLE,
                        detail=(
                            f"检测到环：状态 {target} 经状态 {node} 的符号 "
                            f"{symbol!r} 回到当前 DFS 栈"
                        ),
                    )
                )
                continue
            if target in finished or target in reachable:
                continue
            reachable.add(target)
            on_stack.add(target)
            stack.append((target, sorted(adjacency.get(target, {}))))

    dfs(root_id)

    unreachable = state_ids - reachable
    for sid in sorted(unreachable):
        violations.append(
            IntegrityViolation(
                kind=VIOLATION_UNREACHABLE_STATE,
                detail=f"状态 {sid} 存在但从根状态 {root_id} 不可达",
            )
        )

    # ---- 词数一致性（仅在无环且无悬空时才可严格重算）--------------------
    edges_consistent = not any(
        v.kind == VIOLATION_DANGLING_EDGE for v in violations
    )
    if not has_cycle and edges_consistent:
        expected_counts = _recompute_counts(root_id, adjacency, finals)
        for sid in sorted(state_ids):
            stored = counts.get(sid)
            if stored is None:
                violations.append(
                    IntegrityViolation(
                        kind=VIOLATION_COUNT_MISSING,
                        detail=f"状态 {sid} 缺少持久化词数",
                    )
                )
                continue
            expected = expected_counts.get(sid, 0)
            if stored != expected:
                violations.append(
                    IntegrityViolation(
                        kind=VIOLATION_COUNT_MISMATCH,
                        detail=(
                            f"状态 {sid} 持久化词数 {stored} 与按结构重算值 "
                            f"{expected} 不一致（终结={finals.get(sid)}）"
                        ),
                    )
                )

    return violations


def _recompute_counts(
    root_id: int,
    adjacency: dict[int, dict[str, int]],
    finals: dict[int, bool],
) -> dict[int, int]:
    """后序重算每状态词数；调用前提：无环、无悬空。"""
    result: dict[int, int] = {}

    def postorder(node: int) -> None:
        stack: list[tuple[int, int]] = [(node, 0)]
        order: list[int] = []
        seen: set[int] = set()
        while stack:
            current, index = stack[-1]
            children = list(adjacency.get(current, {}).values())
            if index < len(children):
                stack[-1] = (current, index + 1)
                child = children[index]
                if child not in seen:
                    seen.add(child)
                    stack.append((child, 0))
            else:
                order.append(current)
                stack.pop()
        for nid in order:
            total = 1 if finals.get(nid) else 0
            for child in adjacency.get(nid, {}).values():
                total += result.get(child, 0)
            result[nid] = total

    postorder(root_id)
    return result
