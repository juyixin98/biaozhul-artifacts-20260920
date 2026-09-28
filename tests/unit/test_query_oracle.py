"""End-to-end tests of the query service against the INDEPENDENT oracle.

The oracle rebuilds every expected answer with its own fresh ICU collator and
plain Python (never the store/SQL), so these tests compare the indexed
implementation against an independently written reference, item by item.
They assert concrete results and concrete failure categories — never merely
"the endpoint is callable".
"""
from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from collsvc.collation.options import CollationOptions
from collsvc.collation.versioning import InvalidCursor, VersionMismatchError
from collsvc.corpus.loader import load_corpus
from collsvc.index.store import IndexStore
from collsvc.query.service import PREFIX_COLLATION, PREFIX_TEXT, QueryService
from collsvc.query.validator import IndependentOracle, OracleRow
from collsvc.telemetry import Trace

CORPUS = Path(__file__).resolve().parents[1] / "fixtures"


def _oracle_rows(store) -> list[OracleRow]:
    rows = store.all_rows_ordered()
    return [OracleRow(doc_id=r["doc_id"], text=r["text"], seq=int(r["seq"])) for r in rows]


@pytest.fixture
def built(tmp_path):
    spec = load_corpus(Path(__file__).resolve().parents[2] / "data" / "corpus" / "demo.json")

    def _build(**kw):
        import hashlib

        options = CollationOptions(**kw)
        digest = hashlib.sha1(
            repr(sorted(kw.items())).encode()
        ).hexdigest()[:12]
        store = IndexStore(tmp_path / f"idx-{digest}.db", options)
        store.build(spec)
        return store, IndependentOracle(options, store.expected_version.as_token())

    return _build


# ---------- sorted listing ----------

@pytest.mark.parametrize("locale,strength,numeric,case_first", [
    ("en_US", 1, False, "default"),
    ("en_US", 2, False, "default"),
    ("en_US", 3, False, "default"),
    ("en_US", 3, True, "default"),
    ("en_US", 3, False, "upper"),
    ("tr_TR", 3, False, "default"),
])
def test_sorted_matches_oracle(built, locale, strength, numeric, case_first):
    store, oracle = built(locale=locale, strength=strength, numeric=numeric,
                          case_first=case_first)
    service = QueryService(store)
    page = service.list_sorted(500, None, Trace())
    observed = [r["doc_id"] for r in page.rows]
    expected = oracle.expected_sorted(_oracle_rows(store))
    oracle.assert_doc_id_order(observed, expected, "sorted listing")


def test_sorted_keys_are_monotonic_and_well_formed(built):
    store, _ = built(locale="en_US", strength=3)
    page = QueryService(store).list_sorted(500, None, Trace())
    keys = [bytes(r["sort_key"]) for r in page.rows]
    IndependentOracle.assert_monotonic(keys)
    for key in keys:
        IndependentOracle.assert_key_shape(key)


# ---------- concrete accent / numeric / case content ----------

def test_accent_group_concrete_order(built):
    store, _ = built(locale="en_US", strength=3)
    page = QueryService(store).list_sorted(500, None, Trace())
    texts = [r["text"] for r in page.rows if r["doc_id"].startswith("acc-cote")]
    # NFC and NFD spellings interleave (same key), but base-letter order is fixed.
    assert texts[0] == "cote"
    # every distinct raw spelling is present — identity not lost
    assert len(page.rows) == store.total_count()


def test_numeric_digit_run_concrete_order(built):
    store, _ = built(locale="en_US", strength=3, numeric=True)
    page = QueryService(store).list_sorted(500, None, Trace())
    files = [r["text"] for r in page.rows if r["text"].startswith("file")]
    assert files == ["file1", "file2", "file02", "file10", "file20"]


def test_turkish_concrete_order(built):
    store, _ = built(locale="tr_TR", strength=3)
    page = QueryService(store).list_sorted(500, None, Trace())
    tr = [r["text"] for r in page.rows if r["doc_id"].startswith("tr-")]
    # The demo corpus carries these 8 Turkish items (golden ORDER_TURKISH_TR
    # additionally includes "sağlam"/"SİZ", which are not in this corpus).
    assert tr == ["ağır", "çay", "ırmak", "İstanbul", "öğle",
                  "sıcak", "şapka", "üç"]
    from fixtures.golden import ORDER_TURKISH_TR
    assert set(tr) <= set(ORDER_TURKISH_TR)
    assert [t for t in ORDER_TURKISH_TR if t in set(tr)] == tr


# ---------- value range uses sort keys, not UTF-8 ----------

def test_range_e_to_f_includes_accented_e(built):
    store, oracle = built(locale="en_US", strength=3)
    service = QueryService(store)
    page = service.range_between("e", "f", 500, None, Trace())
    texts = {r["text"] for r in page.rows}
    assert "étude" in texts and "élève" in texts
    assert "zebra" not in texts
    expected = oracle.expected_range(_oracle_rows(store), "e", "f")
    oracle.assert_doc_id_order([r["doc_id"] for r in page.rows], expected, "range e..f")


def test_range_diverges_from_utf8_byte_order(built):
    store, _ = built(locale="en_US", strength=3)
    service = QueryService(store)
    page = service.range_between("e", "f", 500, None, Trace())
    # The exact failure category a UTF-8 boundary would cause:
    IndependentOracle.assert_utf8_would_misclassify("e", "étude", "f")
    assert any(r["text"] == "étude" for r in page.rows)


def test_range_is_inclusive_and_swap_tolerant(built):
    store, oracle = built(locale="en_US", strength=3)
    service = QueryService(store)
    forwards = service.range_between("apple", "banana", 500, None, Trace())
    backwards = service.range_between("banana", "apple", 500, None, Trace())
    f_ids = [r["doc_id"] for r in forwards.rows]
    b_ids = [r["doc_id"] for r in backwards.rows]
    assert f_ids == b_ids  # normalized to low..high
    expected = oracle.expected_range(_oracle_rows(store), "apple", "banana")
    oracle.assert_doc_id_order(f_ids, expected, "range apple..banana")


# ---------- prefix retrieval vs oracle ----------

@pytest.mark.parametrize("prefix", ["cote", "file", "a", "tr", "s", "é", ""])
def test_collation_prefix_matches_oracle(built, prefix):
    store, oracle = built(locale="en_US", strength=3)
    service = QueryService(store)
    page = service.prefix_search(prefix, PREFIX_COLLATION, 500, None, Trace())
    expected = oracle.expected_collation_prefix(_oracle_rows(store), prefix)
    oracle.assert_doc_id_order(
        [r["doc_id"] for r in page.rows], expected, f"collation prefix {prefix!r}"
    )


@pytest.mark.parametrize("prefix", ["cote", "file", "a", "é", "C", "FİL"])
def test_text_prefix_matches_oracle(built, prefix):
    store, oracle = built(locale="tr_TR", strength=3)
    service = QueryService(store)
    page = service.prefix_search(prefix, PREFIX_TEXT, 500, None, Trace())
    expected = oracle.expected_text_prefix(_oracle_rows(store), prefix)
    oracle.assert_doc_id_order(
        [r["doc_id"] for r in page.rows], expected, f"text prefix {prefix!r}"
    )


def test_collation_prefix_is_accent_case_insensitive_at_primary(built):
    store, _ = built(locale="en_US", strength=1)
    service = QueryService(store)
    page = service.prefix_search("COTE", PREFIX_COLLATION, 500, None, Trace())
    texts = {r["text"] for r in page.rows}
    # At primary strength, accent + case differences match.
    assert {"cote", "coté", "côte", "côté"} <= texts


def test_nfc_nfd_prefix_both_match_and_keep_identity(built):
    store, _ = built(locale="en_US", strength=3)
    service = QueryService(store)
    nfc = "côté"
    nfd = unicodedata.normalize("NFD", nfc)
    page_nfc = service.prefix_search(nfc, PREFIX_TEXT, 500, None, Trace())
    page_nfd = service.prefix_search(nfd, PREFIX_TEXT, 500, None, Trace())
    ids_nfc = {r["doc_id"] for r in page_nfc.rows}
    ids_nfd = {r["doc_id"] for r in page_nfd.rows}
    assert ids_nfc == ids_nfd  # same documents...
    # ...and both NFC and NFD raw spellings are returned (identity preserved).
    spellings = {r["text"] for r in page_nfc.rows}
    assert nfc in spellings and nfd in spellings
    assert len(ids_nfc) >= 2


# ---------- numeric prefix uses degraded full scan but stays correct ----------

def test_numeric_prefix_degrades_but_matches_oracle(built):
    store, oracle = built(locale="en_US", strength=3, numeric=True)
    service = QueryService(store)
    trace = Trace()
    page = service.prefix_search("file", PREFIX_COLLATION, 500, None, trace)
    expected = oracle.expected_collation_prefix(_oracle_rows(store), "file")
    oracle.assert_doc_id_order(
        [r["doc_id"] for r in page.rows], expected, "numeric collation prefix"
    )
    assert page.degraded is True
    assert trace.uncertainties, "degraded scan must be recorded as an uncertainty"
    assert trace.uncertainties[0]["detail"]["reason"] == (
        "numeric_collation_merges_digit_weights"
    )


# ---------- stability: equal keys keep input order across pages ----------

def test_equal_keys_are_stable(built):
    store, _ = built(locale="en_US", strength=1)
    service = QueryService(store)
    full = service.list_sorted(500, None, Trace())
    # The two identical duplicate texts must both appear, in input seq order.
    dupes = [r for r in full.rows if r["text"] == "stability-probe"]
    assert [r["doc_id"] for r in dupes] == ["dup-stable-a", "dup-stable-b"]
    # NFC/NFD equal-key spellings stay grouped with ascending seq.
    cote = [r for r in full.rows if r["text"] in {"côte", "côte"}]
    seqs = [int(r["seq"]) for r in cote]
    assert seqs == sorted(seqs)


def test_pagination_is_gapless_and_stable(built):
    store, _ = built(locale="en_US", strength=3)
    service = QueryService(store)
    seen: list[str] = []
    cursor = None
    for _ in range(20):
        page = service.list_sorted(7, cursor, Trace())
        seen.extend(r["doc_id"] for r in page.rows)
        cursor = page.next_cursor
        if cursor is None:
            break
    full = service.list_sorted(500, None, Trace())
    assert seen == [r["doc_id"] for r in full.rows]  # no gaps, no dupes, stable


# ---------- version conflicts and cursor rules ----------

def test_opening_index_with_changed_options_is_conflict(tmp_path):
    spec = load_corpus(Path(__file__).resolve().parents[2] / "data" / "corpus" / "demo.json")
    db = tmp_path / "v.db"
    IndexStore(db, CollationOptions(locale="en_US", strength=3)).build(spec)
    changed = IndexStore(db, CollationOptions(locale="en_US", strength=3, numeric=True))
    with pytest.raises(VersionMismatchError):
        QueryService(changed)


def test_old_cursor_rejected_after_rebuild(tmp_path):
    spec = load_corpus(Path(__file__).resolve().parents[2] / "data" / "corpus" / "demo.json")
    db = tmp_path / "v.db"
    store = IndexStore(db, CollationOptions(locale="en_US", strength=3))
    store.build(spec)
    page = QueryService(store).list_sorted(3, None, Trace())
    old_cursor = page.next_cursor

    # Rules change -> rebuild bumps the version.
    rebuilt = IndexStore(db, CollationOptions(locale="en_US", strength=2))
    rebuilt.build(spec, replace=True)
    service2 = QueryService(rebuilt)
    with pytest.raises(VersionMismatchError):
        service2.list_sorted(3, old_cursor, Trace())


def test_cursor_mode_cannot_be_replayed(built):
    store, _ = built(locale="en_US", strength=3)
    service = QueryService(store)
    page = service.list_sorted(3, None, Trace())
    with pytest.raises(InvalidCursor):
        service.range_between("a", "z", 3, page.next_cursor, Trace())
    with pytest.raises(InvalidCursor):
        service.prefix_search("a", PREFIX_COLLATION, 3, page.next_cursor, Trace())


def test_garbage_cursor_rejected(built):
    store, _ = built(locale="en_US", strength=3)
    service = QueryService(store)
    with pytest.raises(InvalidCursor):
        service.list_sorted(3, "not-a-real-cursor!!!", Trace())
