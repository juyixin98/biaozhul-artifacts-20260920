"""Tests for the SQL fixture loader."""
import pytest

from app.provenance.loader import FixtureLoadError, load_sql_directory, load_sql_text
from app.provenance.store import EvidenceStore

SQL = """
CREATE TABLE employees (
    eid TEXT, dept TEXT, __row_id TEXT, __weight REAL
);
INSERT INTO employees VALUES
    ('alice', 'Eng', 'e1', 2.0),
    ('bob',   NULL,  'e2', 3.0);

CREATE TABLE grants (eid TEXT, project TEXT);
INSERT INTO grants VALUES ('alice', 'P1');
"""


def test_load_sql_text_reflects_tables_and_nulls(tmp_path):
    store = EvidenceStore(tmp_path / "ev.db")
    version_id = load_sql_text(SQL, store, label="seed")

    emp = store.fetch_relation(version_id, "employees")
    assert emp.columns == ("eid", "dept")  # reserved columns stripped
    assert emp.rows[0].row_id == "e1"
    assert emp.rows[0].weight == 2.0
    assert emp.rows[0].values == ("alice", "Eng")
    # NULL preserved
    assert emp.rows[1].values == ("bob", None)

    grants = store.fetch_relation(version_id, "grants")
    # synthetic row id and default weight
    assert grants.rows[0].row_id == "grants#1"
    assert grants.rows[0].weight == 1.0
    assert grants.rows[0].values == ("alice", "P1")


def test_load_directory_orders_files_and_groups_into_one_version(tmp_path):
    (tmp_path / "01_first.sql").write_text(SQL)
    (tmp_path / "02_second.sql").write_text(
        "CREATE TABLE extra (k TEXT); INSERT INTO extra VALUES ('v');"
    )
    store = EvidenceStore(tmp_path / "ev.db")
    version_id = load_sql_directory(tmp_path, "*.sql", store, label="dir")
    assert set(store.list_relations(version_id)) == {"employees", "grants", "extra"}


def test_duplicate_table_across_files_rejected(tmp_path):
    (tmp_path / "01.sql").write_text(SQL)
    (tmp_path / "02.sql").write_text(
        "CREATE TABLE IF NOT EXISTS employees (eid TEXT); "
        "INSERT INTO employees VALUES ('x');"
    )
    store = EvidenceStore(tmp_path / "ev.db")
    with pytest.raises(FixtureLoadError) as exc:
        load_sql_directory(tmp_path, "*.sql", store, label="dup")
    assert exc.value.category == "DUPLICATE_RELATION"


def test_reserved_row_id_uniqueness_enforced(tmp_path):
    bad = """
    CREATE TABLE t (a TEXT, __row_id TEXT);
    INSERT INTO t VALUES ('x', 'same'), ('y', 'same');
    """
    store = EvidenceStore(tmp_path / "ev.db")
    with pytest.raises(FixtureLoadError) as exc:
        load_sql_text(bad, store, label="badid")
    assert exc.value.category == "DUPLICATE_ROW_ID"


def test_empty_directory_is_an_error(tmp_path):
    store = EvidenceStore(tmp_path / "ev.db")
    with pytest.raises(FixtureLoadError) as exc:
        load_sql_directory(tmp_path, "*.sql", store, label="empty")
    assert exc.value.category == "NO_FIXTURES"
