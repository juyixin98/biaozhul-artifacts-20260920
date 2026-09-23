"""Finality gadget core: weighted tally, equivocation handling, safety alarms.

All quorum arithmetic is integer-only:

    finalized  <=>  3 * power(value) > 2 * total_weight

The validator set and ``total_weight`` are frozen when an epoch is created and
never change afterwards. A validator caught signing two distinct values at the
same (epoch, height) has *both* signed messages stored as evidence and its
whole weight is excluded for that epoch — it contributes to neither side.

Once a value is finalized at a position it is immutable. A second,
independently valid quorum certificate for a different value at the same
position raises a :data:`FINALITY_CONFLICT` alarm and freezes the epoch. A
newer message can never overwrite a finalized value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import crypto
from .db import Store

# Alarm / exclusion reason constants.
EQUIVOCATION = "equivocation:double_vote"
FINALITY_CONFLICT = "finality_conflict"
DUPLICATE = "duplicate_vote"


class ServiceError(Exception):
    """An error that maps cleanly onto an HTTP response."""

    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


@dataclass
class ValidatorSpec:
    validator_id: str
    weight: int
    public_key: str


@dataclass
class Tally:
    """Live, deterministic view of one (epoch, height) computed from storage."""

    epoch_id: int
    height: int
    total_weight: int
    required_power: int
    powers: dict[str, int] = field(default_factory=dict)
    counted_votes: dict[str, str] = field(default_factory=dict)  # validator -> value
    excluded: dict[str, dict[str, Any]] = field(default_factory=dict)
    finalized_value: str | None = None
    finalized_power: int | None = None
    frozen: bool = False

    def meets_quorum(self, value: str) -> bool:
        return 3 * self.powers.get(value, 0) > 2 * self.total_weight


class FinalityService:
    def __init__(self, store: Store, chain_id: str):
        self.store = store
        self.chain_id = chain_id

    # ============================================================ epochs

    def create_epoch(
        self, epoch_id: int, height: int, validators: list[ValidatorSpec]
    ) -> dict[str, Any]:
        if epoch_id < 0 or height < 0:
            raise ServiceError(422, "invalid_index", "epoch id and height must be >= 0")
        if not validators:
            raise ServiceError(422, "empty_validator_set", "an epoch needs at least one validator")

        ids = [v.validator_id for v in validators]
        if len(set(ids)) != len(ids):
            raise ServiceError(422, "duplicate_validator", "validator ids must be unique")
        keys = [v.public_key for v in validators]
        if len(set(keys)) != len(keys):
            raise ServiceError(422, "duplicate_key", "public keys must be unique")

        total = 0
        for v in validators:
            if v.weight < 0:
                raise ServiceError(422, "negative_weight", f"weight of {v.validator_id} is negative")
            # Validate the key material now so a broken key cannot vote later.
            try:
                crypto.decode_public_key(v.public_key)
            except ValueError as exc:
                raise ServiceError(
                    422, "invalid_public_key", f"{v.validator_id}: {exc}"
                ) from exc
            total += v.weight

        with self.store.transaction() as conn:
            existing = self.store.get_epoch(epoch_id)
            if existing is not None:
                raise ServiceError(
                    409, "epoch_exists", f"epoch {epoch_id} already exists (set is frozen)"
                )
            # A frozen predecessor means the chain must not advance past it.
            if epoch_id > 0:
                prev = self.store.get_epoch(epoch_id - 1)
                if prev is not None and prev["frozen"]:
                    raise ServiceError(
                        409,
                        "predecessor_frozen",
                        f"epoch {epoch_id - 1} is frozen by an open alarm; cannot advance",
                    )

            self.store.create_epoch(conn, epoch_id, height, total)
            for v in validators:
                self.store.add_validator(
                    conn, epoch_id, v.validator_id, v.public_key, v.weight
                )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('chain_id', ?)",
                (self.chain_id,),
            )

        return {
            "epoch_id": epoch_id,
            "height": height,
            "total_weight": total,
            "validator_count": len(validators),
            "required_power": required_power(total),
        }

    # ============================================================ voting

    def submit_vote(
        self, epoch_id: int, height: int, validator_id: str, value: str, signature: str
    ) -> dict[str, Any]:
        epoch = self._require_epoch(epoch_id)
        self._require_not_frozen(epoch)
        if height != epoch["height"]:
            raise ServiceError(
                422,
                "height_mismatch",
                f"epoch {epoch_id} only accepts votes at height {epoch['height']}",
            )

        validator = self.store.fetchone(
            "SELECT * FROM validators WHERE epoch_id = ? AND validator_id = ?",
            (epoch_id, validator_id),
        )
        if validator is None:
            raise ServiceError(403, "not_validator", f"{validator_id} is not in epoch {epoch_id} set")

        if not crypto.verify_vote(
            validator["public_key"], signature, self.chain_id, epoch_id, height, value
        ):
            raise ServiceError(400, "bad_signature", "vote signature failed Ed25519 verification")

        result: dict[str, Any] = {
            "epoch_id": epoch_id,
            "height": height,
            "validator_id": validator_id,
            "value": value,
            "duplicate": False,
            "equivocation": False,
            "finalized": False,
        }

        with self.store.transaction() as conn:
            prior = conn.execute(
                "SELECT * FROM votes WHERE epoch_id = ? AND height = ? AND validator_id = ?"
                " ORDER BY seq",
                (epoch_id, height, validator_id),
            ).fetchall()
            prior_values = {row["value"] for row in prior}

            seq = self.store.next_vote_seq(conn, epoch_id)
            self.store.insert_vote(
                conn, epoch_id, height, validator_id, value, signature, seq
            )

            if value in prior_values:
                # Same validator signing the same value again: counted once.
                result["duplicate"] = True
            elif prior:
                # A different value after the validator already voted at this height.
                self.store.exclude_validator(
                    conn,
                    epoch_id,
                    validator_id,
                    height,
                    EQUIVOCATION,
                    vote_a_seq=prior[0]["seq"],
                    vote_b_seq=seq,
                )
                result["equivocation"] = True
                result["excluded"] = validator_id

            self._apply_finality(conn, epoch_id, height)

        tally = self.get_tally(epoch_id, height)
        result["finalized"] = tally.finalized_value is not None
        result["finalized_value"] = tally.finalized_value
        result["powers"] = tally.powers
        return result

    def submit_conflicting_certificate(
        self,
        epoch_id: int,
        height: int,
        value: str,
        signatures: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Verify an external quorum certificate and detect a finality fork.

        A real divergence detector receives signed certificates from peers and
        compares them against what it finalized locally. Every signature here
        is verified against the epoch's frozen validator set; the certificate
        stands on its own (distinct signers weighted against the fixed total).
        """
        epoch = self._require_epoch(epoch_id)
        if height != epoch["height"]:
            raise ServiceError(422, "height_mismatch", "certificate height does not match the epoch")
        if not signatures:
            raise ServiceError(422, "empty_certificate", "a certificate needs signatures")

        validators = {
            row["validator_id"]: row
            for row in self.store.validators_for(epoch_id)
        }
        seen: set[str] = set()
        verified_signers: list[dict[str, Any]] = []
        for item in signatures:
            vid = item.get("validator_id")
            sig = item.get("signature")
            if not vid or not sig:
                raise ServiceError(422, "malformed_signature", "each entry needs validator_id and signature")
            if vid not in validators:
                raise ServiceError(403, "not_validator", f"{vid} is not in epoch {epoch_id} set")
            if vid in seen:
                # A certificate counts each validator at most once.
                continue
            if not crypto.verify_vote(
                validators[vid]["public_key"], sig, self.chain_id, epoch_id, height, value
            ):
                raise ServiceError(400, "bad_signature", f"certificate signature from {vid} failed verification")
            seen.add(vid)
            verified_signers.append({"validator_id": vid, "weight": validators[vid]["weight"]})

        cert_power = sum(s["weight"] for s in verified_signers)
        total = epoch["total_weight"]
        checkpoint = self.store.get_checkpoint(epoch_id, height)

        outcome: dict[str, Any] = {
            "epoch_id": epoch_id,
            "height": height,
            "certificate_value": value,
            "certificate_power": cert_power,
            "total_weight": total,
            "required_power": required_power(total),
            "signer_count": len(verified_signers),
        }

        if checkpoint is None or checkpoint["value"] == value:
            outcome["status"] = "agreement"
            # Also feed these through normal processing so equivocations by
            # signers who previously voted differently are recorded.
            self._ingest_certificate_votes(epoch_id, height, value, signatures)
            return outcome

        # Local checkpoint exists for a DIFFERENT value.
        if 3 * cert_power > 2 * total:
            evidence = [
                {
                    "validator_id": s["validator_id"],
                    "weight": s["weight"],
                    "signature": next(
                        i["signature"] for i in signatures if i["validator_id"] == s["validator_id"]
                    ),
                }
                for s in verified_signers
            ]
            with self.store.transaction() as conn:
                alarm_id = self.store.raise_alarm(
                    conn,
                    FINALITY_CONFLICT,
                    epoch_id,
                    height,
                    {
                        "finalized_value": checkpoint["value"],
                        "finalized_power": checkpoint["power"],
                        "conflicting_value": value,
                        "conflicting_power": cert_power,
                        "total_weight": total,
                        "evidence": evidence,
                    },
                )
            outcome.update(
                status="finality_conflict",
                alarm_id=alarm_id,
                finalized_value=checkpoint["value"],
                frozen=True,
            )
            return outcome

        outcome["status"] = "challenge_below_quorum"
        return outcome

    def _ingest_certificate_votes(
        self, epoch_id: int, height: int, value: str, signatures: list[dict[str, str]]
    ) -> None:
        for item in signatures:
            try:
                self.submit_vote(
                    epoch_id, height, item["validator_id"], value, item["signature"]
                )
            except ServiceError:
                # Frozen / already-processed: the certificate verdict itself is
                # what matters and has already been returned to the caller.
                continue

    # ============================================================ finality

    def _apply_finality(self, conn, epoch_id: int, height: int) -> None:
        """Must run inside a transaction. Decide finality from stored state."""
        if conn.execute(
            "SELECT 1 FROM checkpoints WHERE epoch_id = ? AND height = ?",
            (epoch_id, height),
        ).fetchone():
            return  # Already finalized: immutable, never recompute/overwrite.

        tally = self._compute_tally(conn, epoch_id, height)
        quorate = [value for value in tally.powers if 3 * tally.powers[value] > 2 * tally.total_weight]

        if len(quorate) > 1:
            # Defense in depth: two quorums at one position is a hard fork.
            self.store.raise_alarm(
                conn,
                FINALITY_CONFLICT,
                epoch_id,
                height,
                {
                    "values": sorted(quorate),
                    "powers": {v: tally.powers[v] for v in quorate},
                    "total_weight": tally.total_weight,
                },
            )
            return

        if len(quorate) == 1:
            value = quorate[0]
            self.store.finalize(
                conn,
                epoch_id,
                height,
                value,
                tally.powers[value],
                tally.total_weight,
            )

    # ============================================================ queries

    def get_tally(self, epoch_id: int, height: int | None = None) -> Tally:
        epoch = self._require_epoch(epoch_id)
        height = epoch["height"] if height is None else height
        with self.store.transaction() as conn:
            return self._compute_tally(conn, epoch_id, height)

    def _compute_tally(self, conn, epoch_id: int, height: int) -> Tally:
        epoch = conn.execute("SELECT * FROM epochs WHERE epoch_id = ?", (epoch_id,)).fetchone()
        validators = conn.execute(
            "SELECT * FROM validators WHERE epoch_id = ?", (epoch_id,)
        ).fetchall()
        weights = {row["validator_id"]: row["weight"] for row in validators}
        total = int(epoch["total_weight"])

        excluded_rows = conn.execute(
            "SELECT * FROM exclusions WHERE epoch_id = ?", (epoch_id,)
        ).fetchall()
        excluded = {row["validator_id"]: dict(row) for row in excluded_rows}

        counted: dict[str, str] = {}
        rows = conn.execute(
            "SELECT * FROM votes WHERE epoch_id = ? AND height = ? ORDER BY seq",
            (epoch_id, height),
        ).fetchall()
        for row in rows:  # first signed value wins; duplicates collapse
            counted.setdefault(row["validator_id"], row["value"])

        powers: dict[str, int] = {}
        for vid, value in counted.items():
            if vid in excluded:
                continue  # equivocator's weight counts for nobody
            powers[value] = powers.get(value, 0) + weights.get(vid, 0)

        checkpoint = conn.execute(
            "SELECT * FROM checkpoints WHERE epoch_id = ? AND height = ?",
            (epoch_id, height),
        ).fetchone()

        return Tally(
            epoch_id=epoch_id,
            height=height,
            total_weight=total,
            required_power=required_power(total),
            powers=powers,
            counted_votes=counted,
            excluded=excluded,
            finalized_value=checkpoint["value"] if checkpoint else None,
            finalized_power=checkpoint["power"] if checkpoint else None,
            frozen=bool(epoch["frozen"]),
        )

    def epoch_status(self, epoch_id: int) -> dict[str, Any]:
        epoch = self._require_epoch(epoch_id)
        tally = self.get_tally(epoch_id)
        validators = [
            {"validator_id": r["validator_id"], "weight": r["weight"], "public_key": r["public_key"]}
            for r in self.store.validators_for(epoch_id)
        ]
        open_alarms = [a for a in self.list_alarms(epoch_id) if not a["resolved"]]
        return {
            "epoch_id": epoch_id,
            "height": epoch["height"],
            "total_weight": tally.total_weight,
            "required_power": tally.required_power,
            "frozen": tally.frozen,
            "finalized_value": tally.finalized_value,
            "finalized_power": tally.finalized_power,
            "powers": tally.powers,
            "counted_votes": tally.counted_votes,
            "excluded": sorted(tally.excluded),
            "validators": validators,
            "open_alarms": open_alarms,
        }

    def list_alarms(self, epoch_id: int | None = None) -> list[dict[str, Any]]:
        out = []
        for row in self.store.alarms(epoch_id):
            item = dict(row)
            try:
                item["detail"] = json.loads(item["detail"])
            except (TypeError, ValueError):
                pass
            out.append(item)
        return out

    def evidence_for(self, epoch_id: int) -> list[dict[str, Any]]:
        """Return every exclusion with both signed votes attached."""
        self._require_epoch(epoch_id)
        result = []
        for exc in self.store.exclusions_for(epoch_id):
            votes = self.store.fetchall(
                "SELECT value, signature, seq, received_at FROM votes"
                " WHERE epoch_id = ? AND height = ? AND validator_id = ? ORDER BY seq",
                (epoch_id, exc["height"], exc["validator_id"]),
            )
            result.append(
                {
                    "validator_id": exc["validator_id"],
                    "height": exc["height"],
                    "reason": exc["reason"],
                    "detected_at": exc["detected_at"],
                    "signed_votes": [dict(v) for v in votes],
                }
            )
        return result

    def rebuild_conclusions(self) -> dict[str, Any]:
        """Recompute every epoch from raw persisted votes and cross-check.

        Run at startup (and exposed for tests) to prove a restart reconstructs
        the identical conclusion.
        """
        report: dict[str, Any] = {"epochs": []}
        for epoch in self.store.list_epochs():
            epoch_id = epoch["epoch_id"]
            tally = self.get_tally(epoch_id)
            stored = self.store.get_checkpoint(epoch_id, epoch["height"])
            consistent = True
            if tally.finalized_value is not None:
                consistent = (
                    stored is not None
                    and stored["value"] == tally.finalized_value
                    and 3 * stored["power"] > 2 * stored["total_weight"]
                )
            report["epochs"].append(
                {
                    "epoch_id": epoch_id,
                    "frozen": tally.frozen,
                    "finalized_value": tally.finalized_value,
                    "powers": tally.powers,
                    "excluded": sorted(tally.excluded),
                    "consistent": consistent,
                }
            )
        return report

    # ============================================================ helpers

    def _require_epoch(self, epoch_id: int):
        epoch = self.store.get_epoch(epoch_id)
        if epoch is None:
            raise ServiceError(404, "unknown_epoch", f"epoch {epoch_id} does not exist")
        return epoch

    def _require_not_frozen(self, epoch) -> None:
        if epoch["frozen"]:
            raise ServiceError(
                409,
                "epoch_frozen",
                f"epoch {epoch['epoch_id']} is frozen by a finality alarm; no votes are processed",
                {"open_alarms": [a for a in self.list_alarms(epoch["epoch_id"]) if not a["resolved"]]},
            )


def required_power(total_weight: int) -> int:
    """Smallest integer strictly greater than ``2/3 * total_weight``."""
    return (2 * total_weight) // 3 + 1
