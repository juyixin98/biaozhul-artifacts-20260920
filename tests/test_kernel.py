"""Mining kernel: correctness of LCS, boundary safety, ties, overlaps, coverage."""

from conftest import make_kernel

from lcs_batch.kernel import build_lcp, build_suffix_array


# -- suffix array / LCP primitives against naive references -------------------

def naive_suffix_array(symbols):
    return sorted(range(len(symbols)), key=lambda i: symbols[i:])


def naive_lcp(symbols, sa):
    out = [0] * len(sa)
    for k in range(1, len(sa)):
        a, b = sa[k - 1], sa[k]
        n = 0
        while a + n < len(symbols) and b + n < len(symbols) and symbols[a + n] == symbols[b + n]:
            n += 1
        out[k] = n
    return out


def test_suffix_array_matches_naive_on_binary_text():
    symbols = [2, 0, 2, 0, 1, 0, 0, 2, 1, 256]
    sa = build_suffix_array(symbols)
    assert sa == naive_suffix_array(symbols)
    assert build_lcp(symbols, sa) == naive_lcp(symbols, sa)


def test_suffix_array_handles_empty_and_single():
    assert build_suffix_array([]) == []
    assert build_suffix_array([7]) == [0]


# -- repeated single document --------------------------------------------------

def test_identical_documents_yield_whole_document():
    kernel = make_kernel([b"abcabc", b"abcabc", b"abcabc"])
    result = kernel.longest_common_substrings(min_docs=2, max_candidates=10)
    assert result.max_length == 6
    assert [c.substring for c in result.candidates] == [b"abcabc"]
    assert result.candidates[0].doc_coverage == (0, 1, 2)
    assert [(o.doc_index, o.offset) for o in result.candidates[0].occurrences] == [
        (0, 0), (1, 0), (2, 0),
    ]


# -- document boundary safety ---------------------------------------------------

def test_match_never_crosses_document_boundary():
    # Without separators the concatenation "aaaa" would contain "aaa" twice
    # (offsets 0 and 1); with document semantics the answer is "aa".
    kernel = make_kernel([b"aa", b"aa"])
    result = kernel.longest_common_substrings(min_docs=2, max_candidates=10)
    assert result.max_length == 2
    assert [c.substring for c in result.candidates] == [b"aa"]


def test_all_occurrences_stay_inside_their_document():
    docs = [b"abXab", b"abYab", b"zzab"]
    kernel = make_kernel(docs)
    result = kernel.longest_common_substrings(min_docs=2, max_candidates=10)
    assert result.max_length == 2  # "ab"
    for candidate in result.candidates:
        for occ in candidate.occurrences:
            assert occ.offset + candidate.length <= len(docs[occ.doc_index])


# -- binary content --------------------------------------------------------------

def test_binary_bytes_including_nul_and_ff():
    kernel = make_kernel([b"\x00\xff\x00", b"\xff\x00\xff"])
    result = kernel.longest_common_substrings(min_docs=2, max_candidates=10)
    assert result.max_length == 2
    assert [c.substring for c in result.candidates] == [b"\x00\xff", b"\xff\x00"]


def test_separator_value_bytes_are_ordinary_content():
    # 0xFF is the largest content byte; separator symbols live at >= 256 and
    # must not be confused with it.
    kernel = make_kernel([b"\xff\xff\xff", b"\xff\xff"])
    result = kernel.longest_common_substrings(min_docs=2, max_candidates=10)
    assert result.max_length == 2
    assert [c.substring for c in result.candidates] == [b"\xff\xff"]


# -- tied longest candidates: complete enumeration, stable order -----------------

def test_tied_longest_substrings_all_reported_in_stable_order():
    kernel = make_kernel([b"abcXYZdef", b"abcQQdef"])
    result = kernel.longest_common_substrings(min_docs=2, max_candidates=10)
    assert result.max_length == 3
    substrings = [c.substring for c in result.candidates]
    assert substrings == [b"abc", b"def"]  # sorted by bytes: deterministic
    assert substrings == sorted(substrings)
    for candidate in result.candidates:
        assert candidate.doc_coverage == (0, 1)


def test_stable_order_is_byte_order_not_discovery_order():
    # "zzz" appears before "aaa" in both documents, but "aaa" sorts first.
    kernel = make_kernel([b"zzz1aaa", b"zzz2aaa"])
    result = kernel.longest_common_substrings(min_docs=2, max_candidates=10)
    assert [c.substring for c in result.candidates] == [b"aaa", b"zzz"]


# -- overlapping occurrences ------------------------------------------------------

def test_overlapping_occurrences_all_reported():
    # "aa" occurs in "aaaa" at offsets 0, 1, 2 (overlapping).
    kernel = make_kernel([b"aaaa", b"aa"])
    result = kernel.longest_common_substrings(min_docs=2, max_candidates=10)
    assert result.max_length == 2
    (candidate,) = result.candidates
    assert candidate.substring == b"aa"
    assert [(o.doc_index, o.offset) for o in candidate.occurrences] == [
        (0, 0), (0, 1), (0, 2), (1, 0),
    ]


# -- coverage measured in distinct documents --------------------------------------

def test_coverage_counts_documents_not_occurrences():
    # "ab" occurs 3 times in doc0 alone, but coverage must reach across docs.
    kernel = make_kernel([b"ababab", b"ab"])
    result = kernel.longest_common_substrings(min_docs=2, max_candidates=10)
    assert result.max_length == 2
    assert result.candidates[0].doc_coverage == (0, 1)


def test_min_docs_selects_different_answers():
    kernel = make_kernel([b"abc", b"abc", b"abd"])
    two = kernel.longest_common_substrings(min_docs=2, max_candidates=10)
    assert two.max_length == 3
    assert two.candidates[0].substring == b"abc"
    assert two.candidates[0].doc_coverage == (0, 1)

    three = kernel.longest_common_substrings(min_docs=3, max_candidates=10)
    assert three.max_length == 2
    assert three.candidates[0].substring == b"ab"
    assert three.candidates[0].doc_coverage == (0, 1, 2)


def test_no_common_substring_returns_empty():
    kernel = make_kernel([b"abc", b"def"])
    result = kernel.longest_common_substrings(min_docs=2, max_candidates=10)
    assert result.max_length == 0
    assert result.candidates == ()
    assert result.truncated is False


def test_truncation_flag_when_ties_exceed_max_candidates():
    kernel = make_kernel([b"abcXYZdef", b"abcQQdef"])
    result = kernel.longest_common_substrings(min_docs=2, max_candidates=1)
    assert result.truncated is True
    assert len(result.candidates) == 1
    assert result.candidates[0].substring == b"abc"  # first in stable order
