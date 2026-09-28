"""Independent acceptance verification.

The reference answers here do NOT come from the code under test:

* input rows are hard-coded in this module (they mirror fixtures/*.sql),
* the expected *numeric* answer of every query is computed by an independent,
  deliberately naive weighted bag-semantics enumerator written below,
* the expected *symbolic* polynomials for the headline answers are hand-derived
  and written out literally.

The service under test loads the SAME business data through its own SQL loader
and computes symbolic provenance.  We then require, for every answer row:

    hand_symbolic_polynomial == service_polynomial            (where hand-derived)
    service_polynomial.evaluate(weights) == naive_enumerator (every row)

This catches a symbolically-wrong-but-coincidentally-working engine because the
two implementations share no code or generated fixtures.
"""
from __future__ import annotations

import operator
from pathlib import Path

import pytest

from app.config import Settings
from app.provenance.loader import load_sql_directory
from app.provenance.service import QueryService
from app.provenance.store import EvidenceStore

# ---------------------------------------------------------------------------
# Independent hard-coded input (mirror of fixtures/data/01_company.sql).
# Each row: (business values..., witness id, weight)
# ---------------------------------------------------------------------------

EMP_COLS = ("eid", "dept")
EMP = [
    (("alice", "Eng"), "e1", 2.0),
    (("alice", "Eng"), "e6", 9.0),   # value-duplicate of e1
    (("bob", "Eng"), "e2", 3.0),
    (("carol", "Sales"), "e3", 5.0),
    (("dave", None), "e4", 7.0),     # NULL dept
]
DEPT_COLS = ("dept", "budget")
DEPT = [
    (("Eng", 100), "d1", 11.0),
    (("Sales", 200), "d2", 13.0),
]
LEAD_COLS = ("eid", "dept")
LEAD = [
    (("alice", "Eng"), "l1", 17.0),
]

WEIGHTS = (
    {f"emp.{wid}": w for _v, wid, w in EMP}
    | {f"dept.{wid}": w for _v, wid, w in DEPT}
    | {f"lead.{wid}": w for _v, wid, w in LEAD}
)

CMP = {"=": operator.eq, "!=": operator.ne, "<": operator.lt,
       "<=": operator.le, ">": operator.gt, ">=": operator.ge}


# ---------------------------------------------------------------------------
# Independent naive weighted bag-semantics interpreter (no engine imports).
# A "bag" item is (labeled-values, witness-tuple, numeric-value).
# ---------------------------------------------------------------------------

def naive_scan(rows, cols, alias):
    out = []
    for values, wid, w in rows:
        labeled = {f"{alias}.{c}": v for c, v in zip(cols, values)}
        out.append((labeled, (wid,), w))
    return out


def naive_select(items, col, op, const):
    out = []
    for labeled, witnesses, w in items:
        v = labeled[col]
        if v is None:
            continue
        if CMP[op](v, const):
            out.append((labeled, witnesses, w))
    return out


def naive_join(left, right, pairs):
    out = []
    for ll, lw, lval in left:
        for rl, rw, rval in right:
            if all(ll[a] is not None and rl[b] is not None and ll[a] == rl[b]
                   for a, b in pairs):
                out.append(({**ll, **rl}, lw + rw, lval * rval))
    return out


def naive_project(items, columns):
    acc = {}
    for labeled, witnesses, w in items:
        key = tuple(labeled[c] for c in columns)
        acc[key] = acc.get(key, 0) + w
    return acc


def naive_union(left_proj, right_proj):
    acc = dict(left_proj)
    for key, w in right_proj.items():
        acc[key] = acc.get(key, 0) + w
    return acc


# ---------------------------------------------------------------------------
# Build the SUT service from the real SQL fixture directory.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def service_version(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("acc")
    store = EvidenceStore(tmp / "ev.db")
    fixture_dir = Path(__file__).resolve().parent.parent / "fixtures" / "data"
    version_id = load_sql_directory(fixture_dir, "*.sql", store, label="accept")
    settings = Settings(
        db_path=str(tmp / "ev.db"),
        fixture_dir=str(fixture_dir),
        fixture_glob="*.sql",
        log_level="INFO",
        max_query_nodes=200,
        max_witnesses_per_answer=10000,
    )
    return QueryService(store), version_id


def _run(service_version, query):
    service, version_id = service_version
    return service.run(query, version_id)


# ---------------------------------------------------------------------------
# Query A: projection(selection(join)) UNION projection  - exercises all 4 ops
# ---------------------------------------------------------------------------

QUERY_A = {
    "op": "union",
    "left": {
        "op": "project",
        "columns": ["l.eid"],
        "input": {
            "op": "join",
            "left": {
                "op": "select",
                "condition": {"col": "l.dept", "op": "=", "value": "Eng"},
                "input": {"op": "relation", "relation": "emp", "alias": "l"},
            },
            "right": {"op": "relation", "relation": "dept", "alias": "d"},
            "on": [["l.dept", "d.dept"]],
        },
    },
    "right": {"op": "project", "columns": ["lead.eid"],
              "input": {"op": "relation", "relation": "lead"}},
}


def _naive_a():
    emp_scan = naive_scan(EMP, EMP_COLS, "l")
    eng = naive_select(emp_scan, "l.dept", "=", "Eng")
    dept_scan = naive_scan(DEPT, DEPT_COLS, "d")
    joined = naive_join(eng, dept_scan, [("l.dept", "d.dept")])
    left = naive_project(joined, ["l.eid"])
    lead_scan = naive_scan(LEAD, LEAD_COLS, "lead")
    right = naive_project(lead_scan, ["lead.eid"])
    return naive_union(left, right)


def test_query_a_numeric_matches_independent_enumerator(service_version):
    result = _run(service_version, QUERY_A)
    expected = _naive_a()
    got = {tuple(r["values"]): r for r in result["rows"]}
    assert set(got) == set(expected)
    for key, row in got.items():
        symbolic_value = _eval_terms(row["expression"], WEIGHTS)
        assert symbolic_value == expected[key], key
    # concrete totals
    assert expected[("alice",)] == 2 * 11 + 9 * 11 + 17  # 138
    assert expected[("bob",)] == 3 * 11                  # 33


def test_query_a_headline_polynomial_is_hand_derived(service_version):
    result = _run(service_version, QUERY_A)
    got = {tuple(r["values"]): r["provenance"] for r in result["rows"]}
    # two join bindings (e1*d1, e6*d1) PLUS the union witness l1;
    # rendering is canonical: lowest-degree term first
    assert got[("alice",)] == "lead.l1 + dept.d1*emp.e1 + dept.d1*emp.e6"
    assert got[("bob",)] == "dept.d1*emp.e2"
    # Sales employee carol and NULL-dept dave are correctly excluded
    assert ("carol",) not in got
    assert ("dave",) not in got


# ---------------------------------------------------------------------------
# Query B: self-join of emp on dept - duplicate rows + squared witnesses
# ---------------------------------------------------------------------------

QUERY_B = {
    "op": "project",
    "columns": ["l.eid", "r.eid"],
    "input": {
        "op": "join",
        "left": {"op": "relation", "relation": "emp", "alias": "l"},
        "right": {"op": "relation", "relation": "emp", "alias": "r"},
        "on": [["l.dept", "r.dept"]],
    },
}


def _naive_b():
    left = naive_scan(EMP, EMP_COLS, "l")
    right = naive_scan(EMP, EMP_COLS, "r")
    joined = naive_join(left, right, [("l.dept", "r.dept")])
    return naive_project(joined, ["l.eid", "r.eid"])


def test_query_b_self_join_numeric_and_square(service_version):
    result = _run(service_version, QUERY_B)
    expected = _naive_b()
    got = {tuple(r["values"]): r for r in result["rows"]}
    assert set(got) == set(expected)
    for key, row in got.items():
        assert _eval_terms(row["expression"], WEIGHTS) == expected[key]

    # (alice, alice) has four ordered Eng bindings among the two duplicate
    # alice rows; the two cross bindings canonicalize into coefficient 2.
    assert got[("alice", "alice")]["provenance"] == (
        "emp.e1^2 + 2*emp.e1*emp.e6 + emp.e6^2"
    )
    # the diagonal row joined with itself is genuinely squared, not collapsed
    assert got[("bob", "bob")]["provenance"] == "emp.e2^2"
    assert got[("carol", "carol")]["provenance"] == "emp.e3^2"
    # NULL dept produces no self-join pairing at all
    assert ("dave", "dave") not in got
    # numeric spot checks
    assert expected[("bob", "bob")] == 3 * 3
    assert expected[("alice", "alice")] == (
        2 * 2 + 2 * 9 + 9 * 2 + 9 * 9
    )  # 4+18+18+81 = 121


# ---------------------------------------------------------------------------
# Failure categories are asserted concretely, not just "endpoint callable".
# ---------------------------------------------------------------------------

def test_failure_categories_are_specific(service_version):
    service, version_id = service_version
    cases = [
        ({"op": "relation", "relation": "nonexistent"}, "UNKNOWN_RELATION"),
        ({"op": "select",
          "condition": {"col": "emp.nope", "op": "=", "value": 1},
          "input": {"op": "relation", "relation": "emp"}}, "UNKNOWN_COLUMN"),
        ({"op": "select",
          "condition": {"col": "emp.dept", "op": "=", "value": None},
          "input": {"op": "relation", "relation": "emp"}},
         "NULL_PREDICATE_NOT_SUPPORTED"),
        ({"op": "bogus"}, "UNSUPPORTED_OPERATOR"),
    ]
    for query, category in cases:
        with pytest.raises(Exception) as exc:
            service.run(query, version_id)
        assert getattr(exc.value, "category", None) == category, query


def test_answer_and_provenance_share_one_version(service_version):
    service, version_id = service_version
    # loading a second, different version must not contaminate the first answer
    second = load_sql_directory(
        Path(__file__).resolve().parent.parent / "fixtures" / "data",
        "*.sql", EvidenceStore(_db_path(service)), label="accept-again"
    )
    assert second != version_id
    result = service.run(QUERY_A, version_id)  # pin the ORIGINAL version
    assert result["version_id"] == version_id
    assert any(tuple(r["values"]) == ("bob",) for r in result["rows"])


# helpers --------------------------------------------------------------------

def _db_path(service):
    return service.store.db_path


def _eval_terms(expression, weights):
    """Evaluate the serialized polynomial independently of Polynomial."""
    total = 0.0
    for term in expression["terms"]:
        product = 1.0
        for witness in term["witnesses"]:
            product *= weights[witness]
        total += term["coefficient"] * product
    return total
