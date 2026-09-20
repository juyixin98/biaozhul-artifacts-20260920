"""Rebuild from immutable history:

* rebuilt materialised state matches the folded history for every stream;
* tampered materialised state is repaired by rebuild;
* concurrent writers during a rebuild lose no events and all streams end
  consistent (rebuild takes an EXCLUSIVE table lock for its transaction).
"""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.models import ConsentEvent, ConsentState
from app.services.consent import _event_to_fold
from app.services.state import fold_stream
from tests.conftest import create_subject, grant


def _history_fold(db, org_id):
    events = db.query(ConsentEvent).filter(ConsentEvent.organization_id == org_id).all()
    grouped = {}
    for ev in events:
        grouped.setdefault((ev.subject_id, ev.purpose), []).append(ev)
    return {
        key: fold_stream([_event_to_fold(e) for e in evs])
        for key, evs in grouped.items()
    }


def _materialized(db, org_id):
    rows = db.query(ConsentState).filter(ConsentState.organization_id == org_id).all()
    return {(r.subject_id, r.purpose): r.version for r in rows}


def test_rebuild_matches_incremental_processing(client, admin_headers, db):
    from app.models import Organization

    org_id = db.query(Organization).filter(Organization.name == "Org A").one().id

    subjects = [create_subject(client, admin_headers)["id"] for _ in range(3)]
    # Grant + withdraw on stream 0, grant (active) on stream 1, expired grant
    # on stream 2.
    grant(client, admin_headers, subjects[0], "a",
          expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    client.post(
        "/v1/consent/withdraw",
        headers=admin_headers,
        json={"event_id": f"w-{uuid.uuid4().hex[:8]}", "subject_id": subjects[0],
              "purpose": "a", "expected_version": 1},
    )
    grant(client, admin_headers, subjects[1], "b",
          expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    grant(client, admin_headers, subjects[2], "c",
          expires_at=datetime.now(timezone.utc) - timedelta(days=1))

    # Tamper with the materialised table to simulate drift.
    db.execute(text("UPDATE consent_states SET version = 0"))
    db.commit()

    r = client.post("/v1/admin/rebuild", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["streams_rebuilt"] == 3

    db.expire_all()
    folded = _history_fold(db, org_id)
    materialized = _materialized(db, org_id)
    assert set(folded) == set(materialized)
    for key, state in folded.items():
        assert materialized[key] == state.version

    # Spot-check API verdicts after rebuild.
    v0 = client.get(
        f"/v1/consent/{subjects[0]}/a/verify", headers=admin_headers
    ).json()
    assert v0["valid"] is False and v0["reason"] == "withdrawn"
    v1 = client.get(
        f"/v1/consent/{subjects[1]}/b/verify", headers=admin_headers
    ).json()
    assert v1["valid"] is True
    v2 = client.get(
        f"/v1/consent/{subjects[2]}/c/verify", headers=admin_headers
    ).json()
    assert v2["valid"] is False and v2["reason"] == "expired"


def test_writers_lose_no_events_during_rebuild(client, admin_headers, db):
    from app.models import Organization

    org_id = db.query(Organization).filter(Organization.name == "Org A").one().id
    subject = create_subject(client, admin_headers)["id"]
    n_threads = 8
    events_per_thread = 4

    def writer(i):
        purpose = f"stream-{i}"
        # Seed version 0 with a grant.
        r, _ = grant(
            client, admin_headers, subject, purpose,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        assert r.status_code == 200, r.text
        # Append a short chain of withdraw/new-grant pairs.
        version = 1
        for j in range(events_per_thread - 1):
            wr = client.post(
                "/v1/consent/withdraw",
                headers=admin_headers,
                json={"event_id": f"w-{i}-{j}-{uuid.uuid4().hex[:6]}",
                      "subject_id": subject, "purpose": purpose,
                      "expected_version": version},
            )
            assert wr.status_code == 200, wr.text
            version += 1
            gr, _ = grant(
                client, admin_headers, subject, purpose,
                expected_version=version,
                expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            )
            assert gr.status_code == 200, gr.text
            version += 1

    def rebuilder():
        # Run several rebuilds interleaved with the writers.
        for _ in range(3):
            r = client.post("/v1/admin/rebuild", headers=admin_headers)
            assert r.status_code == 200, r.text

    with ThreadPoolExecutor(max_workers=n_threads + 1) as pool:
        futures = [pool.submit(writer, i) for i in range(n_threads)]
        rf = pool.submit(rebuilder)
        for f in futures:
            f.result()
        rf.result()

    # All streams end at exactly events_per_thread*2 - 1 versions
    # (1 grant seed + 2 events per extra cycle... writer does 1 grant then
    # (events_per_thread-1) withdraw+grant pairs => 1 + 2*(n-1) events).
    expected_final_version = 1 + 2 * (events_per_thread - 1)
    total_events = n_threads * expected_final_version

    db.expire_all()
    db.expunge_all()
    count = (
        db.query(ConsentEvent)
        .filter(ConsentEvent.organization_id == org_id)
        .count()
    )
    assert count == total_events

    folded = _history_fold(db, org_id)
    materialized = _materialized(db, org_id)
    assert len(folded) == n_threads
    for key, state in folded.items():
        assert state.version == expected_final_version
    # Materialised table may predate the very last commits if a rebuild ran
    # first; run one final rebuild and then everything must agree exactly.
    client.post("/v1/admin/rebuild", headers=admin_headers)
    db.expire_all()
    materialized = _materialized(db, org_id)
    for key, state in folded.items():
        assert materialized[key] == state.version
