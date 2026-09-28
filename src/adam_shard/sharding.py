"""Uneven shard planning over the flat parameter vector.

The final shard absorbs the remainder, so shard sizes differ when the total
element count is not divisible by the process count.  Plans are pure
functions of (total_numel, world_size), making save-side and a
differently-sized restore side agree after re-sharding.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ShardPlan:
    total_numel: int
    world_size: int
    boundaries: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        if self.world_size <= 0:
            raise ValueError("world_size must be positive")
        if self.total_numel <= 0:
            raise ValueError("total_numel must be positive")
        spans = [(s, e) for s, e in self.boundaries]
        if [s for s, _ in spans] != sorted(s for s, _ in spans):
            raise ValueError("shard boundaries out of order")
        if spans[0][0] != 0 or spans[-1][1] != self.total_numel:
            raise ValueError("shard plan must cover [0, total_numel)")
        for (_, e1), (s2, _) in zip(spans, spans[1:]):
            if e1 != s2:
                raise ValueError("shard plan has gaps or overlaps")

    @classmethod
    def create(cls, total_numel: int, world_size: int) -> "ShardPlan":
        if world_size > total_numel:
            raise ValueError(
                f"world_size {world_size} exceeds total_numel {total_numel}; "
                "every rank must own at least one element"
            )
        base, rem = divmod(total_numel, world_size)
        sizes = [base] * world_size
        sizes[-1] += rem  # uneven tail lives on the last rank
        boundaries: list[tuple[int, int]] = []
        cursor = 0
        for size in sizes:
            boundaries.append((cursor, cursor + size))
            cursor += size
        return cls(total_numel=total_numel, world_size=world_size, boundaries=tuple(boundaries))

    def span(self, rank: int) -> tuple[int, int]:
        if not 0 <= rank < self.world_size:
            raise IndexError(f"rank {rank} out of range for world_size {self.world_size}")
        return self.boundaries[rank]

    def sizes(self) -> tuple[int, ...]:
        return tuple(e - s for s, e in self.boundaries)

    def to_manifest(self) -> list[dict]:
        return [
            {"rank": rank, "start": start, "end": end, "numel": end - start}
            for rank, (start, end) in enumerate(self.boundaries)
        ]


def reshard_indices(src: ShardPlan, dst: ShardPlan) -> list[list[tuple[int, int, int, int]]]:
    """Copy map from ``src`` shards to ``dst`` shards over flat indices.

    Returns a list (dst rank) of (src_rank, src_offset, dst_offset, length)
    segments covering the intersection of each destination shard with source
    shards; offsets are local to their owning shard.  Both plans must span the
    same total element range.
    """

    if src.total_numel != dst.total_numel:
        raise ValueError("cannot reshard plans covering different totals")

    segments: list[list[tuple[int, int, int, int]]] = [[] for _ in range(dst.world_size)]
    for d_rank, (d_start, d_end) in enumerate(dst.boundaries):
        cursor = d_start
        for s_rank, (s_start, s_end) in enumerate(src.boundaries):
            lo = max(cursor, s_start)
            hi = min(d_end, s_end)
            if lo < hi:
                segments[d_rank].append((s_rank, lo - s_start, lo - d_start, hi - lo))
                cursor = hi
        if cursor != d_end:
            raise RuntimeError("reshard coverage gap")  # pragma: no cover - defensive
    return segments
