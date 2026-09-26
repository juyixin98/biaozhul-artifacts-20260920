"""有界缓存的流式（在线）配对器。

消息可能乱序到达，两类传感器各自维护一个容量有限的缓存。算法基于
**事件时间水位线（watermark）**工作：

* 每流维护观测到的最大时间戳 ``M``，乱序有界参数 ``δ``
  （``max_out_of_orderness``）：任何“未来才到达”的消息，其时间戳不会
  小于 ``M - δ``。全局水位线取两流之差的最小值
  ``wm = min(M_a - δ, M_b - δ)``。
* 消息 ``m`` 在 ``t_m + tolerance < wm``（严格）时**成熟（ripe）**：
  此后不可能再有容差内的新候选到达。严格不等式是必须的——水位线只
  保证“不会再有时间戳小于 wm 的消息”，而 ``t_m + tolerance == wm``
  时仍可能到达一条距离恰好等于 tolerance 的边界候选。
* 每次水位线推进，在**当前全部缓存消息**上构造候选二分图（候选边
  ``|t_b - t_a| <= tolerance``），并按连通分量结算：一个分量**封闭**
  当且仅当其中所有顶点都成熟（``t + tolerance < wm``）——此时不可能有
  任何新消息再与该分量相连（未来消息时间戳 >= wm），分量内结局已定。
  封闭分量内按与离线定义相同的全局边序做贪心，提交配对；分量内未配对
  的消息以 ``expired_no_candidate`` 原因拒绝；孤立且成熟的顶点同样作为
  单顶点封闭分量拒绝。
* 只看单个顶点成熟是**不够的**：未来消息可能抢走某条候选链上的对端，
  经“增广链”连锁改变成熟消息的归属（例如
  ``a@0—b@10—a'@10.5`` 中 a 虽成熟，但 b'@10.6 的到达会让 a' 改配 b'，
  a 反而应与 b 配对）。必须等整个连通分量封闭。
* 缓存满时新消息直接以 ``buffer_overflow`` 拒绝（不淘汰已缓存数据）。
* 所有消息送入完毕后调用 :meth:`flush`，水位线视为 +∞；仍未配对的消息
  以 ``end_of_stream_unmatched`` 拒绝。

在不发生溢出提前丢弃的前提下，本算法的最终配对与
:func:`sensor_matcher.offline.offline_pair` 完全一致。封闭分量之外的消息
一律不动，因此分量封闭后可能到达的消息只可能与更晚的未封闭顶点相连，
不会回改已提交的结果。
"""

from dataclasses import dataclass, field

from .models import (
    REASON_BUFFER_OVERFLOW,
    REASON_END_UNMATCHED,
    REASON_EXPIRED,
    Match,
    Message,
    Reject,
)


@dataclass(frozen=True)
class StepResult:
    """单次 :meth:`push` 或 :meth:`flush` 新产生的结果。"""

    matches: tuple[Match, ...] = ()
    rejects: tuple[Reject, ...] = ()
    watermark: float = float("-inf")


@dataclass
class _Buffer:
    capacity: int
    items: dict[str, Message] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.items)

    def add(self, msg: Message) -> bool:
        if msg.id in self.items:
            raise ValueError(f"重复的消息 id: {msg.id}")
        if len(self.items) >= self.capacity:
            return False
        self.items[msg.id] = msg
        return True

    def remove(self, ids: set[str]) -> None:
        for mid in ids:
            self.items.pop(mid, None)

    def all(self) -> list[Message]:
        return list(self.items.values())


class MessageMatcher:
    """两传感器流的有界缓存在线配对器。

    Args:
        tolerance: 非负时间容差（秒），|t_b - t_a| <= tolerance 才可能配对。
        max_buffer_size: 每个流缓存的最大消息数。
        max_out_of_orderness: 乱序有界量 δ（秒）。时间戳为 t 的消息最晚
            可在观测最大值达到 t + δ 时才到达。必须非负。
    """

    def __init__(
        self,
        tolerance: float,
        max_buffer_size: int = 1000,
        max_out_of_orderness: float = 0.0,
    ) -> None:
        if tolerance < 0:
            raise ValueError("tolerance 不能为负")
        if max_buffer_size <= 0:
            raise ValueError("max_buffer_size 必须为正整数")
        if max_out_of_orderness < 0:
            raise ValueError("max_out_of_orderness 不能为负")
        self.tolerance = float(tolerance)
        self.max_buffer_size = int(max_buffer_size)
        self.max_out_of_orderness = float(max_out_of_orderness)
        self._buf_a = _Buffer(self.max_buffer_size)
        self._buf_b = _Buffer(self.max_buffer_size)
        self._max_a = float("-inf")
        self._max_b = float("-inf")
        self._closed = False
        self.matches: list[Match] = []
        self.rejects: list[Reject] = []

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #
    def push(self, stream: str, message: Message) -> StepResult:
        """送入一条消息（``stream`` 为 ``"a"`` 或 ``"b"``）。

        时间戳应为已校正到统一时基的值。返回本次调用因水位线推进而新
        提交的配对与拒绝。缓存满时消息被拒绝（原因 buffer_overflow），
        不进入缓存也不参与后续配对。
        """
        if self._closed:
            raise RuntimeError("flush 之后不能再送入消息")
        if stream not in ("a", "b"):
            raise ValueError(f"未知流: {stream!r}，应为 'a' 或 'b'")

        rejects: list[Reject] = []
        buf = self._buf_a if stream == "a" else self._buf_b

        # 无论是否接纳，观测到的最大时间戳都参与水位线：水位线刻画的是
        # “到达完整性”，与消息是否因缓存满被丢弃无关。
        if stream == "a":
            self._max_a = max(self._max_a, message.timestamp)
        else:
            self._max_b = max(self._max_b, message.timestamp)

        accepted = buf.add(message)
        if not accepted:
            rej = Reject(
                stream=stream,
                id=message.id,
                reason=REASON_BUFFER_OVERFLOW,
                timestamp=message.timestamp,
            )
            rejects.append(rej)
            self.rejects.append(rej)
            return StepResult((), tuple(rejects), self._watermark())

        step = self._advance(final=False)
        return StepResult(tuple(step[0]), tuple(rejects + step[1]), self._watermark())

    def flush(self) -> StepResult:
        """声明所有消息均已到达，提交所有待定结果。

        已缓存但无法配对的消息以 ``end_of_stream_unmatched`` 拒绝。
        """
        if self._closed:
            return StepResult((), (), float("inf"))
        self._closed = True
        matches, rejects = self._advance(final=True)
        return StepResult(tuple(matches), tuple(rejects), float("inf"))

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    def _watermark(self) -> float:
        if self._max_a == float("-inf") or self._max_b == float("-inf"):
            return float("-inf")
        return min(
            self._max_a - self.max_out_of_orderness,
            self._max_b - self.max_out_of_orderness,
        )

    def _greedy(self, msgs_a: list[Message], msgs_b: list[Message]):
        """在给定消息集合上按离线边序做贪心，返回 (pairs, paired_id_sets)。

        pairs: [(Message a, Message b), ...]（已按边序接受的顺序）
        paired_a / paired_b: 贪心后仍被占用的 id 集合。
        """
        edges = []
        for ma in msgs_a:
            for mb in msgs_b:
                d = abs(mb.timestamp - ma.timestamp)
                if d <= self.tolerance:
                    edges.append((d, ma, mb))
        # 距离 -> 较早时间戳 -> 较晚时间戳 -> (a_id, b_id)，与离线定义一致
        edges.sort(
            key=lambda e: (
                e[0],
                min(e[1].timestamp, e[2].timestamp),
                max(e[1].timestamp, e[2].timestamp),
                e[1].id,
                e[2].id,
            )
        )
        used_a: set[str] = set()
        used_b: set[str] = set()
        pairs: list[tuple[Message, Message]] = []
        for _d, ma, mb in edges:
            if ma.id in used_a or mb.id in used_b:
                continue
            used_a.add(ma.id)
            used_b.add(mb.id)
            pairs.append((ma, mb))
        return pairs, used_a, used_b

    def _candidate_edges(
        self, msgs_a: list[Message], msgs_b: list[Message]
    ) -> list[tuple[Message, Message]]:
        """返回容差内的全部候选边 (ma, mb)。"""
        edges: list[tuple[Message, Message]] = []
        for ma in msgs_a:
            for mb in msgs_b:
                if abs(mb.timestamp - ma.timestamp) <= self.tolerance:
                    edges.append((ma, mb))
        return edges

    @staticmethod
    def _connected_components(
        msgs_a: list[Message],
        msgs_b: list[Message],
        edges: list[tuple[Message, Message]],
    ) -> list[set[str]]:
        """在候选图上求连通分量（顶点 id 加 'a:'/'b:' 前缀区分两流）。

        孤立顶点各自构成单顶点分量。
        """
        adj: dict[str, set[str]] = {}
        for m in msgs_a:
            adj[f"a:{m.id}"] = set()
        for m in msgs_b:
            adj[f"b:{m.id}"] = set()
        for ma, mb in edges:
            va, vb = f"a:{ma.id}", f"b:{mb.id}"
            adj[va].add(vb)
            adj[vb].add(va)

        seen: set[str] = set()
        components: list[set[str]] = []
        for start in adj:
            if start in seen:
                continue
            stack = [start]
            comp: set[str] = set()
            while stack:
                v = stack.pop()
                if v in seen:
                    continue
                seen.add(v)
                comp.add(v)
                stack.extend(adj[v] - seen)
            components.append(comp)
        return components

    def _advance(self, final: bool):
        """水位线推进后的统一结算。返回 (new_matches, new_rejects)。"""
        new_matches: list[Match] = []
        new_rejects: list[Reject] = []

        wm = float("inf") if final else self._watermark()
        # 严格成熟：t + tol < wm  ⟺  t < wm - tol
        ripe_t = wm - self.tolerance

        msgs_a = self._buf_a.all()
        msgs_b = self._buf_b.all()
        by_id_a = {m.id: m for m in msgs_a}
        by_id_b = {m.id: m for m in msgs_b}
        ripe_a = {m.id for m in msgs_a if m.timestamp < ripe_t}
        ripe_b = {m.id for m in msgs_b if m.timestamp < ripe_t}

        edges = self._candidate_edges(msgs_a, msgs_b)
        components = self._connected_components(msgs_a, msgs_b, edges)

        commit_ids_a: set[str] = set()
        commit_ids_b: set[str] = set()
        unmatched_reason = REASON_END_UNMATCHED if final else REASON_EXPIRED

        for comp in components:
            comp_a = [by_id_a[v[2:]] for v in comp if v.startswith("a:")]
            comp_b = [by_id_b[v[2:]] for v in comp if v.startswith("b:")]
            # 分量封闭 ⟺ 所有顶点都成熟 ⟺ 未来消息不可能再改变其结局
            closed = all(m.id in ripe_a for m in comp_a) and all(
                m.id in ripe_b for m in comp_b
            )
            if not closed:
                continue

            pairs, used_a, used_b = self._greedy(comp_a, comp_b)
            for ma, mb in pairs:
                new_matches.append(
                    Match(
                        id_a=ma.id,
                        id_b=mb.id,
                        t_a=ma.timestamp,
                        t_b=mb.timestamp,
                        raw_a=ma.timestamp,
                        raw_b=mb.timestamp,
                    )
                )
                commit_ids_a.add(ma.id)
                commit_ids_b.add(mb.id)
            for ma in comp_a:
                if ma.id not in used_a:
                    new_rejects.append(
                        Reject("a", ma.id, unmatched_reason, ma.timestamp)
                    )
                    commit_ids_a.add(ma.id)
            for mb in comp_b:
                if mb.id not in used_b:
                    new_rejects.append(
                        Reject("b", mb.id, unmatched_reason, mb.timestamp)
                    )
                    commit_ids_b.add(mb.id)

        self._buf_a.remove(commit_ids_a)
        self._buf_b.remove(commit_ids_b)

        # 输出按 A 侧时间稳定排序
        new_matches.sort(key=lambda m: (m.t_a, m.id_a))
        new_rejects.sort(key=lambda r: (r.timestamp, r.stream, r.id))

        self.matches.extend(new_matches)
        self.rejects.extend(new_rejects)
        return new_matches, new_rejects
