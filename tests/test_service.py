"""Tests for the query service: version pinning and end-to-end evaluation."""
import pytest

from app.provenance.service import QueryService, QueryError
from app.provenance.store import EvidenceStore
from app.provenance.loader import load_sql_text

SQL_A = """
CREATE TABLE R (id TEXT, b INTEGER, __row_id TEXT, __weight REAL);
INSERT INTO R VALUES ('a', 1, 'r1', 2.0), ('a', 2, 'r2', 3.0);
CREATE TABLE S (b INTEGER, c TEXT, __row_id TEXT, __weight REAL);
INSERT INTO S VALUES (1, 'x', 's1', 5.0), (2, 'y', 's2', 7.0);
"""

SQL_B = """
CREATE TABLE R (id TEXT, b INTEGER);
INSERT INTO R VALUES ('zzz', 999);
CREATE TABLE S (b INTEGER, c TEXT);
INSERT INTO S VALUES (999, 'other');
"""

JOIN_QUERY = {
    "op": "join",
    "left": {"op": "relation", "relation": "R", "alias": "l"},
    "right": {"op": "relation", "relation": "S", "alias": "r"},
    "on": [["l.b", "r.b"]],
}


@pytest.fixture
def service(tmp_path):
    store = EvidenceStore(tmp_path / "ev.db")
    v1 = load_sql_text(SQL_A, store, label="A")
    v2 = load_sql_text(SQL_B, store, label="B")
    return QueryService(store), v1, v2


def test_answer_and_provenance_come_from_named_version(service):
    qsvc, v1, v2 = service
    result = qsvc.run(JOIN_QUERY, version_id=v1)
    assert result["version_id"] == v1
    rows = {(tuple(r["values"])): r for r in result["rows"]}
    # version A values
    assert ("a", 1, 1, "x") in rows
    assert rows[("a", 1, 1, "x")]["provenance"] == "R.r1*S.s1"

    # explicitly the other version must return ITS data, not A's
    result_b = qsvc.run(JOIN_QUERY, version_id=v2)
    values_b = [tuple(r["values"]) for r in result_b["rows"]]
    assert values_b == [("zzz", 999, 999, "other")]


def test_latest_version_is_explicitly_resolved(service):
    qsvc, v1, v2 = service
    result = qsvc.run(JOIN_QUERY, version_id=None)
    assert result["version_id"] == v2  # latest, not v1


def test_numeric_verification_matches_expression(service):
    qsvc, v1, _ = service
    result = qsvc.run(JOIN_QUERY, version_id=v1)
    row = next(r for r in result["rows"] if tuple(r["values"]) == ("a", 1, 1, "x"))
    verified = qsvc.verify(result["version_id"], [row])
    assert verified[0]["numeric_value"] == 10.0  # weight(r1)=2, weight(s1)=5
    assert verified[0]["matches"] is True


def test_unknown_version_is_named_error(service):
    qsvc, _v1, _v2 = service
    with pytest.raises(QueryError) as exc:
        qsvc.run(JOIN_QUERY, version_id=123456)
    assert exc.value.category == "UNKNOWN_VERSION"


def test_validation_error_category_propagates(service):
    qsvc, v1, _ = service
    with pytest.raises(QueryError) as exc:
        qsvc.run({"op": "relation", "relation": "ghost"}, version_id=v1)
    assert exc.value.category == "UNKNOWN_RELATION"


def test_missing_weight_verification_is_failure_category(service):
    qsvc, v1, _ = service
    # version A has full weights; force an unknown witness to exercise path
    fake_row = [{
        "values": ["a", 1, 1, "x"],
        "provenance": "R.ghost*S.s1",
        "expression": {"terms": [
            {"witnesses": ["R.ghost", "S.s1"], "coefficient": 1}
        ]},
    }]
    with pytest.raises(QueryError) as exc:
        qsvc.verify(v1, fake_row)
    assert exc.value.category == "MISSING_WEIGHT"
