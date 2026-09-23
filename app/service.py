"""Service layer: evidence aggregation, snapshot freezing and penalty recovery.

Transaction boundaries
----------------------
Evidence detection (vote insert + evidence insert) and penalty application are
**separate transactions**, linked by the evidence status. A crash between them
leaves ``DETECTED``/``AWAITING_SNAPSHOT`` evidence that ``recover_pending()``
finishes on the next ingest, snapshot creation, or process startup.

Concurrency
-----------
Every merge takes a Postgres transaction advisory lock derived from the offense
group (chain, validator, height, round, vote_type), so concurrent deliveries of
the same double sign are fully serialized. UNIQUE constraints on votes, evidence
and penalties are the last line of defense against double punishment.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from . import domain
from .config import settings
from .db import transaction

# Statuses returned to API clients.
ST_DUPLICATE_VOTE = "duplicate_vote"
ST_NO_CONFLICT = "accepted_no_conflict"
ST_ALREADY_PENALIZED = "already_penalized"
ST_PENALIZED = "penalized"
ST_PENDING_SNAPSHOT = "awaiting_snapshot"
ST_DETECTED = "detected"


@dataclass
class IngestResult:
    status: str
    evidence_id: str | None = None
    penalty_id: int | None = None
    detail: str | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "evidence_id": self.evidence_id,
            "penalty_id": self.penalty_id,
            "detail": self.detail,
        }


# ---------------------------------------------------------------- chain setup

def register_chain(chain_id: str) -> None:
    with transaction() as conn:
        conn.execute(
            "INSERT INTO chains (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
            (chain_id,),
        )


def register_validator(chain_id: str, public_key_hex: str, moniker: str = "") -> str:
    public_key = domain.parse_public_key(public_key_hex)
    address = domain.derive_address(public_key)
    with transaction() as conn:
        _ensure_chain(conn, chain_id)
        conn.execute(
            """
            INSERT INTO validators (chain_id, validator_address, moniker, public_key)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (chain_id, validator_address) DO UPDATE
              SET moniker = EXCLUDED.moniker, public_key = EXCLUDED.public_key
            """,
            (chain_id, address, moniker, public_key_hex),
        )
    return address


def put_stake_snapshot(chain_id: str, validator_address: str, epoch: int, voting_power: int) -> dict:
    """Freeze the stake base for an epoch.

    Snapshots are immutable: resubmitting the same (chain, validator, epoch)
    is a no-op that returns the frozen value, and a value referenced by a
    penalty can never be rewritten. Later delegations therefore cannot alter
    the historical base a penalty was computed from.
    """
    if epoch < 0 or voting_power < 0:
        raise domain.DomainError("epoch and voting_power must be non-negative")
    with transaction() as conn:
        _ensure_chain(conn, chain_id)
        _ensure_validator(conn, chain_id, validator_address)
        conn.execute(
            """
            INSERT INTO stake_snapshots (chain_id, validator_address, epoch, voting_power)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (chain_id, validator_address, epoch) DO NOTHING
            """,
            (chain_id, validator_address, epoch, voting_power),
        )
        snapshot_id, frozen = conn.execute(
            """
            SELECT id, voting_power FROM stake_snapshots
            WHERE chain_id = %s AND validator_address = %s AND epoch = %s
            """,
            (chain_id, validator_address, epoch),
        ).fetchone()
        if frozen != voting_power:
            raise domain.DomainError(
                f"snapshot for epoch {epoch} is frozen at {frozen}; "
                f"cannot rewrite with {voting_power}"
            )

    # A freshly supplied snapshot may unblock evidence that was waiting on it.
    blocked = _evidence_blocked_on_epoch(chain_id, validator_address, epoch)
    for evidence_id in blocked:
        apply_penalty(evidence_id)
    return {"snapshot_id": snapshot_id, "epoch": epoch, "voting_power": voting_power}


# ------------------------------------------------------------------- ingestion

def ingest_vote(raw_envelope: dict) -> IngestResult:
    """Verify, store and merge one offline vote; apply a penalty if due."""
    # Structure + address/pubkey consistency (no DB, no signature yet).
    fields, public_key, signature = domain.validate_envelope(raw_envelope)

    group = (fields.chain_id, fields.validator_address, fields.height,
             fields.round, fields.vote_type)

    with transaction() as conn:
        _lock_group(conn, group)

        _ensure_chain(conn, fields.chain_id)
        _ensure_validator(conn, fields.chain_id, fields.validator_address)

        # Real Ed25519 verification over the chain-bound message: a signature
        # made for another chain_id is invalid here.
        domain.verify_signature(fields, public_key, signature)

        inserted = conn.execute(
            """
            INSERT INTO votes
                (chain_id, validator_address, height, round, vote_type,
                 block_hash, public_key, signature, raw_envelope)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (chain_id, validator_address, height, round, vote_type, block_hash)
            DO NOTHING
            RETURNING id
            """,
            (
                fields.chain_id, fields.validator_address, fields.height,
                fields.round, fields.vote_type, fields.block_hash,
                public_key.hex(), signature.hex(),
                json.dumps(raw_envelope, sort_keys=True),
            ),
        ).fetchone()
        is_new_vote = inserted is not None

        evidence_id = _merge_evidence(conn, fields, public_key, signature)

        if evidence_id is None:
            return IngestResult(
                status=ST_NO_CONFLICT if is_new_vote else ST_DUPLICATE_VOTE,
                detail=(
                    "vote stored, no conflicting block hash in this round"
                    if is_new_vote
                    else "identical vote already present; two identical votes are not a double sign"
                ),
            )

    # --- separate transaction: penalty application (crash boundary) ---
    if settings.crash_after_evidence:
        raise RuntimeError(
            "injected crash after evidence commit, before penalty application"
        )

    return apply_penalty(evidence_id)


def _merge_evidence(conn, fields, public_key, signature) -> str | None:
    """Insert evidence for this offense group if a double sign exists.

    Returns the evidence id (existing or newly created), or None when fewer
    than two distinct block hashes are present. Deterministic regardless of
    arrival order: canonical records are sorted before hashing.
    """
    rows = conn.execute(
        """
        SELECT id, block_hash, public_key, signature
        FROM votes
        WHERE chain_id = %s AND validator_address = %s
          AND height = %s AND round = %s AND vote_type = %s
        ORDER BY id
        """,
        (
            fields.chain_id, fields.validator_address, fields.height,
            fields.round, fields.vote_type,
        ),
    ).fetchall()

    earliest_by_hash: dict[str, tuple[int, str, str]] = {}
    for vote_id, block_hash, pub_hex, sig_hex in rows:
        earliest_by_hash.setdefault(block_hash, (vote_id, pub_hex, sig_hex))

    if len(earliest_by_hash) < 2:
        return None

    # Deterministic pair: the two earliest-arriving distinct block hashes.
    pair = sorted(earliest_by_hash.items(), key=lambda kv: kv[1][0])[:2]
    (h1, (first_id, first_pub, first_sig)), (h2, (second_id, second_pub, second_sig)) = pair

    records = [
        domain.canonical_vote_record(
            domain.VoteFields(
                chain_id=fields.chain_id, validator_address=fields.validator_address,
                height=fields.height, round=fields.round, vote_type=fields.vote_type,
                block_hash=h1,
            ),
            bytes.fromhex(first_pub), bytes.fromhex(first_sig),
        ),
        domain.canonical_vote_record(
            domain.VoteFields(
                chain_id=fields.chain_id, validator_address=fields.validator_address,
                height=fields.height, round=fields.round, vote_type=fields.vote_type,
                block_hash=h2,
            ),
            bytes.fromhex(second_pub), bytes.fromhex(second_sig),
        ),
    ]
    evidence_id, canonical_body = domain.evidence_identity(records)

    raw_evidence = {
        "judgment_version": settings.judgment_version,
        "votes": [
            _raw_vote(conn, first_id),
            _raw_vote(conn, second_id),
        ],
    }
    epoch = domain.epoch_of_round(fields.round, settings.rounds_per_epoch)

    # One evidence row per offense group — extra/conflicting late votes can
    # never open a second punishable case.
    conn.execute(
        """
        INSERT INTO evidence
            (id, chain_id, validator_address, height, round, vote_type,
             first_vote_id, second_vote_id, canonical_body, raw_evidence,
             judgment_version, epoch, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'DETECTED')
        ON CONFLICT (chain_id, validator_address, height, round, vote_type)
        DO NOTHING
        """,
        (
            evidence_id, fields.chain_id, fields.validator_address, fields.height,
            fields.round, fields.vote_type, first_id, second_id, canonical_body,
            json.dumps(raw_evidence), settings.judgment_version, epoch,
        ),
    )
    # The content-addressed id is only reachable via RETURNING on insert; for an
    # existing group row fetch its id.
    existing = conn.execute(
        """
        SELECT id FROM evidence
        WHERE chain_id = %s AND validator_address = %s
          AND height = %s AND round = %s AND vote_type = %s
        """,
        (
            fields.chain_id, fields.validator_address, fields.height,
            fields.round, fields.vote_type,
        ),
    ).fetchone()
    return existing[0]


def _raw_vote(conn, vote_id: int) -> dict:
    return conn.execute("SELECT raw_envelope FROM votes WHERE id = %s", (vote_id,)).fetchone()[0]


# -------------------------------------------------------------------- penalty

def apply_penalty(evidence_id: str) -> IngestResult:
    """Move DETECTED/AWAITING evidence to PENALIZED against the frozen snapshot.

    Idempotent and concurrency-safe; safe to call repeatedly (recovery).
    """
    with transaction() as conn:
        _lock_evidence(conn, evidence_id)
        evidence = conn.execute(
            """
            SELECT status, chain_id, validator_address, epoch
            FROM evidence WHERE id = %s FOR UPDATE
            """,
            (evidence_id,),
        ).fetchone()
        if evidence is None:
            raise domain.DomainError(f"unknown evidence {evidence_id}")
        status, chain_id, validator_address, epoch = evidence

        existing = conn.execute(
            "SELECT id FROM penalties WHERE evidence_id = %s", (evidence_id,)
        ).fetchone()
        if existing:
            if status != "PENALIZED":
                conn.execute(
                    "UPDATE evidence SET status='PENALIZED', updated_at=now() WHERE id=%s",
                    (evidence_id,),
                )
            return IngestResult(ST_ALREADY_PENALIZED, evidence_id, existing[0],
                                "this evidence was already penalized; late input changes nothing")

        snapshot = conn.execute(
            """
            SELECT id, voting_power FROM stake_snapshots
            WHERE chain_id = %s AND validator_address = %s AND epoch = %s
            """,
            (chain_id, validator_address, epoch),
        ).fetchone()
        if snapshot is None:
            conn.execute(
                """
                UPDATE evidence
                SET status='AWAITING_SNAPSHOT', updated_at=now()
                WHERE id = %s AND status <> 'AWAITING_SNAPSHOT'
                """,
                (evidence_id,),
            )
            return IngestResult(
                ST_PENDING_SNAPSHOT, evidence_id,
                detail=f"no stake snapshot frozen for epoch {epoch}; penalty deferred",
            )

        snapshot_id, frozen_power = snapshot
        slashed = domain.slash_power(frozen_power, settings.slash_rate_ppm)
        penalty = conn.execute(
            """
            INSERT INTO penalties
                (evidence_id, chain_id, validator_address, epoch, snapshot_id,
                 frozen_power, slash_rate_ppm, slashed_power, judgment_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (evidence_id) DO NOTHING
            RETURNING id
            """,
            (
                evidence_id, chain_id, validator_address, epoch, snapshot_id,
                frozen_power, settings.slash_rate_ppm, slashed,
                settings.judgment_version,
            ),
        ).fetchone()
        conn.execute(
            "UPDATE evidence SET status='PENALIZED', updated_at=now() WHERE id=%s",
            (evidence_id,),
        )
        return IngestResult(
            ST_PENALIZED, evidence_id, penalty[0],
            f"slashed {slashed} of {frozen_power} frozen power (epoch {epoch} snapshot #{snapshot_id})",
        )


def recover_pending() -> dict:
    """Reconcile every non-finalized evidence. Called at startup and via API."""
    with transaction() as conn:
        rows = conn.execute(
            "SELECT id FROM evidence WHERE status <> 'PENALIZED' ORDER BY created_at, id"
        ).fetchall()
    results = [apply_penalty(evidence_id).as_dict() for evidence_id, in rows]
    return {"recovered": len(results), "results": results}


def _evidence_blocked_on_epoch(chain_id: str, validator_address: str, epoch: int) -> list[str]:
    with transaction() as conn:
        rows = conn.execute(
            """
            SELECT id FROM evidence
            WHERE status = 'AWAITING_SNAPSHOT' AND chain_id = %s
              AND validator_address = %s AND epoch = %s
            """,
            (chain_id, validator_address, epoch),
        ).fetchall()
    return [r[0] for r in rows]


# --------------------------------------------------------------------- lookup

def list_evidence(chain_id: str | None = None) -> list[dict]:
    q = """
        SELECT e.id, e.chain_id, e.validator_address, e.height, e.round,
               e.vote_type, e.epoch, e.status, e.judgment_version,
               e.first_vote_id, e.second_vote_id,
               p.id AS penalty_id
        FROM evidence e
        LEFT JOIN penalties p ON p.evidence_id = e.id
    """
    args: tuple = ()
    if chain_id:
        q += " WHERE e.chain_id = %s"
        args = (chain_id,)
    q += " ORDER BY e.created_at"
    with transaction() as conn:
        rows = conn.execute(q, args).fetchall()
    return [
        {
            "evidence_id": r[0], "chain_id": r[1], "validator_address": r[2],
            "height": r[3], "round": r[4], "vote_type": r[5], "epoch": r[6],
            "status": r[7], "judgment_version": r[8],
            "first_vote_id": r[9], "second_vote_id": r[10], "penalty_id": r[11],
        }
        for r in rows
    ]


def get_evidence(evidence_id: str) -> dict | None:
    with transaction() as conn:
        row = conn.execute(
            """
            SELECT id, chain_id, validator_address, height, round, vote_type,
                   epoch, status, judgment_version, canonical_body, raw_evidence
            FROM evidence WHERE id = %s
            """,
            (evidence_id,),
        ).fetchone()
        if row is None:
            return None
        penalty = conn.execute(
            """
            SELECT id, snapshot_id, frozen_power, slash_rate_ppm, slashed_power,
                   epoch, judgment_version, created_at
            FROM penalties WHERE evidence_id = %s
            """,
            (evidence_id,),
        ).fetchone()
    return {
        "evidence_id": row[0], "chain_id": row[1], "validator_address": row[2],
        "height": row[3], "round": row[4], "vote_type": row[5], "epoch": row[6],
        "status": row[7], "judgment_version": row[8],
        "canonical_body_hex": row[9].hex(),
        "raw_evidence": row[10],
        "penalty": None if penalty is None else {
            "penalty_id": penalty[0], "snapshot_id": penalty[1],
            "frozen_power": penalty[2], "slash_rate_ppm": penalty[3],
            "slashed_power": penalty[4], "epoch": penalty[5],
            "judgment_version": penalty[6],
            "created_at": penalty[7].isoformat(),
        },
    }


def list_penalties() -> list[dict]:
    with transaction() as conn:
        rows = conn.execute(
            """
            SELECT id, evidence_id, chain_id, validator_address, epoch,
                   snapshot_id, frozen_power, slash_rate_ppm, slashed_power,
                   judgment_version
            FROM penalties ORDER BY id
            """
        ).fetchall()
    return [
        {
            "penalty_id": r[0], "evidence_id": r[1], "chain_id": r[2],
            "validator_address": r[3], "epoch": r[4], "snapshot_id": r[5],
            "frozen_power": r[6], "slash_rate_ppm": r[7],
            "slashed_power": r[8], "judgment_version": r[9],
        }
        for r in rows
    ]


# ------------------------------------------------------------------- helpers

def _ensure_chain(conn, chain_id: str) -> None:
    exists = conn.execute("SELECT 1 FROM chains WHERE id = %s", (chain_id,)).fetchone()
    if exists is None:
        raise domain.DomainError(f"chain {chain_id!r} is not registered")


def _ensure_validator(conn, chain_id: str, validator_address: str) -> None:
    exists = conn.execute(
        "SELECT 1 FROM validators WHERE chain_id=%s AND validator_address=%s",
        (chain_id, validator_address),
    ).fetchone()
    if exists is None:
        raise domain.DomainError(
            f"validator {validator_address} is not registered on chain {chain_id!r}"
        )


def _lock_group(conn, group) -> None:
    key = "grp:" + "|".join(map(str, group))
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))


def _lock_evidence(conn, evidence_id: str) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                 ("ev:" + evidence_id,))
