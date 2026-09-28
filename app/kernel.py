"""Mining kernel: generalized suffix array, LCP, and longest-common-substring
extraction over the separated symbol stream.

Nothing here is demo-shaped: the suffix array is a real prefix-doubling
construction, the LCP array is Kasai's algorithm, and the coverage-aware
longest-match scan is the classic sliding window over the suffix array
where a window is admissible only while it covers >= ``min_docs`` *distinct
documents* (never mere occurrence counts).
"""

from __future__ import annotations

from collections import deque

from app.corpus import SymbolStream


def build_suffix_array(symbols: list[int]) -> list[int]:
    """Prefix-doubling suffix array over an integer alphabet. O(n log n) rounds."""
    n = len(symbols)
    if n == 0:
        return []
    alphabet = {sym: rank for rank, sym in enumerate(sorted(set(symbols)))}
    rank = [alphabet[sym] for sym in symbols]
    sa = list(range(n))
    sa.sort(key=rank.__getitem__)
    tmp = [0] * n
    step = 1
    while step < n:
        sa.sort(key=lambda i: (rank[i], rank[i + step] if i + step < n else -1))
        tmp[sa[0]] = 0
        classes = 0
        for i in range(1, n):
            prev, cur = sa[i - 1], sa[i]
            prev_key = (rank[prev], rank[prev + step] if prev + step < n else -1)
            cur_key = (rank[cur], rank[cur + step] if cur + step < n else -1)
            if cur_key != prev_key:
                classes += 1
            tmp[cur] = classes
        rank, tmp = tmp, rank
        if classes == n - 1:
            break
        step <<= 1
    return sa


def build_lcp(symbols: list[int], sa: list[int]) -> list[int]:
    """Kasai's LCP construction. lcp[i] = LCP(sa[i-1], sa[i]); lcp[0] = 0."""
    n = len(symbols)
    rank = [0] * n
    for pos, suffix in enumerate(sa):
        rank[suffix] = pos
    lcp = [0] * n
    height = 0
    for i in range(n):
        r = rank[i]
        if r == 0:
            continue
        j = sa[r - 1]
        while i + height < n and j + height < n and symbols[i + height] == symbols[j + height]:
            height += 1
        lcp[r] = height
        if height:
            height -= 1
    return lcp


def longest_common_length(stream: SymbolStream, sa: list[int], lcp: list[int], min_docs: int) -> int:
    """Longest length shared by at least ``min_docs`` distinct documents.

    Sliding window over the suffix array; the window is admissible while the
    suffixes inside it touch >= min_docs distinct documents. The window value
    is the minimum LCP inside it (monotonic deque), except that a degenerate
    single-suffix window (only reachable when min_docs == 1) is worth the
    remaining extent of that suffix within its own document — never beyond,
    so a match can never leak past a separator.
    """
    n = len(sa)
    if n == 0:
        return 0
    best = 0
    counts: dict[int, int] = {}
    distinct = 0
    window_mins: deque[int] = deque()  # lcp indices, values monotonically increasing
    lo = 0
    for hi in range(n):
        doc = stream.doc_of[sa[hi]]
        if doc >= 0:
            if counts.get(doc, 0) == 0:
                distinct += 1
            counts[doc] = counts.get(doc, 0) + 1
        while window_mins and lcp[window_mins[-1]] >= lcp[hi]:
            window_mins.pop()
        window_mins.append(hi)
        # Evict lcp indices that fell out of the window (valid range is lo+1..hi).
        while window_mins and window_mins[0] <= lo:
            window_mins.popleft()
        while distinct >= min_docs and lo <= hi:
            if lo == hi:
                candidate = stream.extent[sa[lo]]
            else:
                candidate = lcp[window_mins[0]]
            if candidate > best:
                best = candidate
            leaving = stream.doc_of[sa[lo]]
            if leaving >= 0:
                counts[leaving] -= 1
                if counts[leaving] == 0:
                    distinct -= 1
            lo += 1
            while window_mins and window_mins[0] <= lo:
                window_mins.popleft()
    return best


def collect_candidates(
    stream: SymbolStream, sa: list[int], lcp: list[int], min_docs: int, length: int
) -> list[bytes]:
    """All distinct substrings of ``length`` covering >= min_docs documents.

    Suffixes sharing a prefix of ``length`` form maximal runs in the suffix
    array delimited by lcp < length. A run answers the query iff its suffixes
    span >= min_docs distinct documents. Ties are returned in stable,
    deterministic order: lexicographic by raw bytes.
    """
    if length <= 0:
        return []
    n = len(sa)
    found: set[bytes] = set()
    i = 0
    while i < n:
        j = i
        docs: set[int] = set()
        doc = stream.doc_of[sa[i]]
        if doc >= 0:
            docs.add(doc)
        while j + 1 < n and lcp[j + 1] >= length:
            j += 1
            doc = stream.doc_of[sa[j]]
            if doc >= 0:
                docs.add(doc)
        if len(docs) >= min_docs and stream.extent[sa[i]] >= length:
            start = sa[i]
            # The shared prefix contains no separator (separators are unique),
            # so every symbol in it is a raw content byte.
            found.add(bytes(stream.symbols[start : start + length]))
        i = j + 1
    return sorted(found)


def find_occurrence_positions(symbols: list[int], sa: list[int], pattern: bytes) -> list[int]:
    """All stream positions where ``pattern`` occurs, via binary search on the SA."""
    n = len(sa)
    size = len(pattern)

    def prefix_compare(suffix_pos: int) -> int:
        for k in range(size):
            sym = symbols[suffix_pos + k] if suffix_pos + k < n else -1
            if sym != pattern[k]:
                return -1 if sym < pattern[k] else 1
        return 0

    lo, hi = 0, n
    while lo < hi:  # first suffix whose length-|pattern| prefix is >= pattern
        mid = (lo + hi) // 2
        if prefix_compare(sa[mid]) < 0:
            lo = mid + 1
        else:
            hi = mid
    first = lo
    lo, hi = first, n
    while lo < hi:  # first suffix whose prefix is > pattern
        mid = (lo + hi) // 2
        if prefix_compare(sa[mid]) <= 0:
            lo = mid + 1
        else:
            hi = mid
    return [sa[i] for i in range(first, lo)]
