"""并行复制串行化（parallel-copy sequencing）。

输入并行复制集合 ``dest <- src``，输出顺序 copy 列表，使每个目的最终得到
并行语义下应有的值。

算法（经典 ready-set + 临时破环）：

1. 自复制 ``a <- a`` 删除；
2. 对剩余复制计算**入度**：目的 ``d`` 的入度 = 它的源同时作为多少个
   待处理复制的源（即有几条复制等着这个值）。等价地，维护
   ``pending(src) = 以 src 为源、尚未发射的复制集合``；
3. **ready**：源不是任何待处理复制目的的复制可立即发射
   （该值不会再被覆盖）；发射后从 pending 移除；
4. 没有 ready 时依赖图（边 src -> dest）必含环。取环上任意一条复制
   ``d <- s``：新临时 ``t``，发射 ``t <- s`` 保存 s 的旧值，把所有
   待处理复制里对旧 s 的读取重定向到 t，环即被打破——原来读旧 s 的
   环外复制也能在 s 被覆盖后正确读到 t。
"""

from __future__ import annotations

from dataclasses import dataclass

from .ir import Const, Name, Operand


@dataclass
class Move:
    dest: str
    src: Operand
    seq: int = -1


def sequentialize(pairs: list[tuple[str, Operand]]) -> list[Move]:
    # 去自复制
    work: dict[str, Operand] = {}
    for d, s in pairs:
        if isinstance(s, Name) and s.name == d:
            continue
        work[d] = s

    out: list[Move] = []
    temp_counter = 0

    while work:
        # ready：目的 d 没有被任何待处理复制当作“源”等待——
        # 即 d 位置的旧值已无人需要，可以安全覆盖。
        live_sources = {s.name for s in work.values() if isinstance(s, Name)}
        ready = next((d for d in work if d not in live_sources), None)
        if ready is not None:
            out.append(Move(ready, work.pop(ready)))
            continue

        # 依赖边 src -> dest：沿“目的的源也是目的”前进找环。
        start = next(iter(work))
        seen: dict[str, int] = {}
        path: list[str] = []
        node = start
        while node not in seen:
            seen[node] = len(path)
            path.append(node)
            node = work[node].name
        cyc = path[seen[node]:]

        # 环中取一条复制 d0 <- s0（s0 是环中后继）。
        # 1) t <- s0 保护源旧值；
        # 2) 所有读旧 s0 的待处理复制（含 d0 自己）重定向到 t；
        #    此后 s0 不再被等待，依赖图被打破，回 ready-set 继续。
        d0 = cyc[0]
        s0 = work[d0].name
        t = f"pcopy.t{temp_counter}"
        temp_counter += 1
        out.append(Move(t, Name(s0)))
        for d in list(work):
            src = work[d]
            if isinstance(src, Name) and src.name == s0:
                work[d] = Name(t)

    return out
