"""Mining kernel: generalized suffix array + LCP longest-common-substring search.

Algorithm outline
-----------------
1. Build the suffix array of the encoded symbol stream by prefix doubling
   (O(n log n)).
2. Build the LCP array with Kasai's algorithm (O(n)) and a sparse table for
   O(1) range-minimum queries.
3. Pass 1 — a sliding window over the suffix array finds the maximum length
   ``L*`` such that some length-``L*`` substring occurs in at least
   ``min_docs`` *distinct* documents. Coverage is counted in documents, never
   in raw occurrences.
4. Pass 2 — maximal SA runs whose interior LCP values are all >= ``L*`` are in
   one-to-one correspondence with the distinct length-``L*`` substrings, so
   tied longest candidates are enumerated completely. Runs covering fewer
   than ``min_docs`` documents are dropped.

All occurrences are reported, including overlapping ones, as
``(doc_index, offset)`` pairs in original document coordinates. Candidates
are ordered deterministically by ``(substring bytes, occurrences)`` so equal
length ties have a stable, reproducible order.
"""

from __future__ import annotations

from dataclasses import dataclass

from .corpus import SEPARATOR_DOC_OF, EncodedCorpus


def build_suffix_array(symbols: tuple[int, ...] | list[int]) -> list[int]:
    """Prefix-doubling suffix array over an integer alphabet."""
    n = len(symbols)
    if n == 0:
        return []
    compression = {value: rank for rank, value in enumerate(sorted(set(symbols)))}
    rank = [compression[s] for s in symbols]
    sa = list(range(n))
    scratch = [0] * n
    length = 1
    while length < n:
        sa.sort(key=lambda i: (rank[i], rank[i + length] if i + length < n else -1))
        scratch[sa[0]] = 0
        for pos in range(1, n):
            prev, cur = sa[pos - 1], sa[pos]
            prev_key = (rank[prev], rank[prev + length] if prev + length < n else -1)
            cur_key = (rank[cur], rank[cur + length] if cur + length < n else -1)
            scratch[cur] = scratch[prev] + (prev_key != cur_key)
        rank, scratch = scratch, rank
        if rank[sa[-1]] == n - 1:
            break
        length <<= 1
    return sa


def build_lcp(symbols: tuple[int, ...] | list[int], sa: list[int]) -> list[int]:
    """Kasai's algorithm; ``lcp[i]`` is the common-prefix length of the
    suffixes at ``sa[i - 1]`` and ``sa[i]`` (``lcp[0] == 0``)."""
    n = len(symbols)
    rank = [0] * n
    for position, suffix in enumerate(sa):
        rank[suffix] = position
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


class _SparseMin:
    """O(1) range-minimum queries over a static array."""

    def __init__(self, values: list[int]) -> None:
        self._log = [0] * (len(values) + 1)
        for i in range(2, len(values) + 1):
            self._log[i] = self._log[i // 2] + 1
        self._table = [list(values)]
        level = 1
        while (1 << level) <= len(values):
            previous = self._table[-1]
            span = 1 << (level - 1)
            self._table.append(
                [
                    min(previous[i], previous[i + span])
                    for i in range(len(values) - (1 << level) + 1)
                ]
            )
            level += 1

    def query(self, start: int, stop: int) -> int:
        """Minimum over the half-open range ``[start, stop)``; start < stop."""
        level = self._log[stop - start]
        row = self._table[level]
        return min(row[start], row[stop - (1 << level)])


@dataclass(frozen=True, order=True)
class Occurrence:
    doc_index: int
    offset: int  # byte offset within the original document


@dataclass(frozen=True)
class LcsCandidate:
    substring: bytes
    occurrences: tuple[Occurrence, ...]  # sorted by (doc_index, offset)

    @property
    def length(self) -> int:
        return len(self.substring)

    @property
    def doc_coverage(self) -> tuple[int, ...]:
        """Distinct document indexes containing the substring, ascending."""
        return tuple(sorted({occ.doc_index for occ in self.occurrences}))


@dataclass(frozen=True)
class KernelResult:
    max_length: int
    candidates: tuple[LcsCandidate, ...]
    truncated: bool
    stats: dict


class SuffixKernel:
    """Read-only mining structure over an :class:`EncodedCorpus`."""

    def __init__(
        self,
        corpus: EncodedCorpus,
        sa: list[int] | None = None,
        lcp: list[int] | None = None,
    ) -> None:
        self._corpus = corpus
        self._sa = sa if sa is not None else build_suffix_array(corpus.symbols)
        self._lcp = lcp if lcp is not None else build_lcp(corpus.symbols, self._sa)
        self._rmq = _SparseMin(self._lcp)

    @property
    def corpus(self) -> EncodedCorpus:
        return self._corpus

    @property
    def suffix_array(self) -> list[int]:
        return list(self._sa)

    @property
    def lcp_array(self) -> list[int]:
        return list(self._lcp)

    def longest_common_substrings(
        self, min_docs: int, max_candidates: int
    ) -> KernelResult:
        """All longest substrings occurring in >= ``min_docs`` distinct docs."""
        if min_docs < 2:
            raise ValueError("min_docs must be >= 2")
        if min_docs > self._corpus.doc_count:
            raise ValueError("min_docs exceeds document count")

        max_length = self._find_max_length(min_docs)
        if max_length == 0:
            return KernelResult(
                max_length=0,
                candidates=(),
                truncated=False,
                stats=self._stats(runs_examined=0, candidates_found=0),
            )

        candidates, runs_examined = self._enumerate_runs(max_length, min_docs)
        candidates.sort(key=lambda c: (c.substring, c.occurrences))
        truncated = len(candidates) > max_candidates
        return KernelResult(
            max_length=max_length,
            candidates=tuple(candidates[:max_candidates]),
            truncated=truncated,
            stats=self._stats(
                runs_examined=runs_examined, candidates_found=len(candidates)
            ),
        )

    # -- pass 1 ------------------------------------------------------------

    def _find_max_length(self, min_docs: int) -> int:
        """Sliding window over the SA: for each right edge, shrink the left
        edge as far as possible while the window still covers >= min_docs
        distinct documents; the window's minimum interior LCP is a candidate
        length."""
        sa, doc_of = self._sa, self._corpus.doc_of
        best = 0
        counts: dict[int, int] = {}
        covered = 0
        left = 0
        for right in range(len(sa)):
            doc = doc_of[sa[right]]
            if doc != SEPARATOR_DOC_OF:
                if counts.get(doc, 0) == 0:
                    covered += 1
                counts[doc] = counts.get(doc, 0) + 1
            while covered >= min_docs:
                if right > left:
                    window_min = self._rmq.query(left + 1, right + 1)
                    if window_min > best:
                        best = window_min
                leaving = doc_of[sa[left]]
                if leaving != SEPARATOR_DOC_OF:
                    counts[leaving] -= 1
                    if counts[leaving] == 0:
                        covered -= 1
                left += 1
        return best

    # -- pass 2 ------------------------------------------------------------

    def _enumerate_runs(
        self, length: int, min_docs: int
    ) -> tuple[list[LcsCandidate], int]:
        """Maximal SA runs with interior LCP >= ``length`` map one-to-one to
        the distinct longest substrings."""
        sa, lcp, doc_of = self._sa, self._lcp, self._corpus.doc_of
        doc_starts = self._corpus.doc_starts
        symbols = self._corpus.symbols
        candidates: list[LcsCandidate] = []
        runs_examined = 0
        run_start = 0
        for i in range(1, len(sa) + 1):
            if i < len(sa) and lcp[i] >= length:
                continue
            run_stop = i  # run is [run_start, run_stop)
            runs_examined += 1
            if run_stop - run_start >= 2:
                occurrences = tuple(
                    sorted(
                        Occurrence(doc, sa[j] - doc_starts[doc])
                        for j in range(run_start, run_stop)
                        if (doc := doc_of[sa[j]]) != SEPARATOR_DOC_OF
                    )
                )
                if len({occ.doc_index for occ in occurrences}) >= min_docs:
                    start = sa[run_start]
                    substring = bytes(symbols[start : start + length])
                    candidates.append(
                        LcsCandidate(substring=substring, occurrences=occurrences)
                    )
            run_start = i
        return candidates, runs_examined

    def _stats(self, runs_examined: int, candidates_found: int) -> dict:
        return {
            "suffix_count": len(self._sa),
            "doc_count": self._corpus.doc_count,
            "runs_examined": runs_examined,
            "candidates_found": candidates_found,
        }
