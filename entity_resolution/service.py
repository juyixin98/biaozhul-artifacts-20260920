"""Application facade: orchestrates the ER pipeline.

Pipeline (each boundary hands over an explicit contract):

    corpus spec
      -> normalization (AliasIndex + PreparedRecord)
      -> similarity    (per-pair evidence, soft weights)
      -> clustering    (constraint validation then constrained solve)
      -> storage       (persist assignment, preserving locks)
      -> change impact (affected entities before/after)

Every public method opens a :class:`RunJournal` so inputs, intermediate state,
decisions and the outcome/error category are replayable from the run id.
"""

from __future__ import annotations

from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any

from .clustering import SolverConfig, canon_pair, solve
from .diagnostics import RunJournal, new_run_id
from .errors import (
    EmptyCorpusError,
    EntityResolutionError,
    InvalidRequestError,
    LockViolationError,
    NoSuchClusterError,
)
from .models import (
    AffectedEntities,
    ClusterOut,
    ClusterSolution,
    CorpusIn,
    LinkIn,
    LinkResult,
    PairEvidence,
    RecordOut,
)
from .normalization import AliasIndex
from .similarity import (
    SimilarityConfig,
    build_candidates,
    prepare_record,
)
from .storage import Storage


def _cluster_id(index: int) -> str:
    # Stable, human-readable ids; the index order is deterministic (sorted ids).
    return f"cl{index + 1:04d}"


class EntityResolutionService:
    def __init__(
        self,
        storage: Storage,
        similarity: SimilarityConfig | None = None,
        solver: SolverConfig | None = None,
        log_dir: str | Path = "logs",
    ) -> None:
        self.storage = storage
        self.sim_cfg = similarity or SimilarityConfig()
        self.solver_cfg = solver or SolverConfig()
        self.log_dir = Path(log_dir)

    # ------------------------------------------------------------- internals

    def _journal(self, operation: str) -> RunJournal:
        return RunJournal(
            run_id=new_run_id(operation),
            operation=operation,
            log_dir=self.log_dir,
        )

    def _load(self) -> tuple[list, AliasIndex, set, set, list[frozenset[str]]]:
        rows = self.storage.list_records()
        if not rows:
            raise EmptyCorpusError("corpus is empty; load records first")
        aliases = AliasIndex(self.storage.list_aliases())
        must, cannot = self.storage.list_links()
        locked = self.storage.locked_blocks()
        prepared = [
            prepare_record(r["id"], r["name"], r["language"], r["attributes"])
            for r in rows
        ]
        return prepared, aliases, must, cannot, locked

    @staticmethod
    def _weights(candidates: dict) -> dict[tuple[str, str], float]:
        # Feed *every* pair score to the global objective, not just candidates:
        # a low score is active split evidence (within-cluster cost 1-w), which
        # is what lets the optimum break a similarity chain. The threshold only
        # labels the ``candidate`` evidence flag; it never drives a union-find.
        return {pair: ps.score for pair, ps in candidates.items()}

    # ------------------------------------------------------------- corpus API

    def load_corpus(self, corpus: CorpusIn) -> dict[str, Any]:
        journal = self._journal("load")
        try:
            if not corpus.records:
                raise EmptyCorpusError("corpus must contain at least one record")
            ids = [r.id for r in corpus.records]
            if len(set(ids)) != len(ids):
                dupes = sorted({x for x in ids if ids.count(x) > 1})
                raise InvalidRequestError(
                    "duplicate record ids", {"duplicates": dupes}
                )

            # Build the alias index up front so a contradictory alias table is
            # rejected before we touch storage.
            alias_index = AliasIndex(corpus.aliases)
            must = [canon_pair(a, b) for a, b in corpus.must_links]
            cannot = [canon_pair(a, b) for a, b in corpus.cannot_links]

            journal.stage(
                "normalized",
                record_count=len(corpus.records),
                aliases=len(alias_index),
                must=must,
                cannot=cannot,
            )
            # Validate constraints against the corpus before persisting.
            from .clustering import validate_constraints

            validate_constraints(ids, must, cannot)

            written = self.storage.replace_corpus(
                [r.model_dump() for r in corpus.records],
                must,
                cannot,
                corpus.aliases,
            )
            journal.succeed(records=written)
            return {"records": written, "run_id": journal.run_id}
        except EntityResolutionError as exc:
            journal.fail(exc.category, exc.code.value, exc.message, exc.details)
            raise
        finally:
            journal.flush()

    # ------------------------------------------------------------- solve API

    def resolve(self, threshold: float | None = None) -> ClusterSolution:
        journal = self._journal("resolve")
        try:
            sim_cfg = (
                dataclass_replace(self.sim_cfg, threshold=threshold)
                if threshold is not None
                else self.sim_cfg
            )
            prepared, aliases, must, cannot, locked = self._load()
            record_ids = [p.id for p in prepared]

            candidates = build_candidates(prepared, sim_cfg, aliases)
            weights = self._weights(candidates)
            journal.stage(
                "candidates",
                pairs={
                    f"{a}|{b}": {
                        "score": round(ps.score, 4),
                        "token_overlap": round(ps.token_overlap, 4),
                        "char_similarity": round(ps.char_similarity, 4),
                        "alias_match": ps.alias_match,
                        "candidate": ps.candidate,
                        "vetoed": ps.vetoed,
                    }
                    for (a, b), ps in sorted(candidates.items())
                },
            )
            for pair, ps in sorted(candidates.items()):
                if ps.vetoed:
                    journal.decide(
                        f"{pair[0]}~{pair[1]}",
                        "reject_hard_attribute_conflict",
                        conflicting=ps.conflicting_attributes,
                    )
                elif ps.candidate:
                    journal.decide(
                        f"{pair[0]}~{pair[1]}",
                        "soft_candidate",
                        score=round(ps.score, 4),
                    )

            result = solve(
                record_ids,
                weights,
                must=must,
                cannot=cannot,
                locked_blocks=locked,
                config=self.solver_cfg,
            )
            journal.stage(
                "solved",
                method=result.method,
                optimal=result.optimal,
                partitions_evaluated=result.partitions_evaluated,
                blocks=[sorted(c) for c in result.clusters],
            )

            # Persist, preserving locks; diff to report affected entities.
            before = self.storage.assignment()
            locked_id_by_member = self._locked_id_map()
            previous_by_members = self._previous_unlocked_by_members()
            plan = self._plan_cluster_ids(
                result,
                locked_id_by_member,
                previous_by_members,
                self._next_auto_index(),
            )
            assignments: dict[str, str] = {}
            for cid, members, _locked in plan:
                self.storage.ensure_cluster(cid)
                for member in members:
                    assignments[member] = cid

            self.storage.apply_solution(assignments, journal.run_id)
            after = self.storage.assignment()

            changed = sorted(rid for rid in record_ids if before.get(rid) != after.get(rid))
            journal.decide(
                "assignment",
                "applied_with_locks_preserved",
                changed_records=changed,
                locked_records=sorted(locked_id_by_member),
            )

            solution = self._build_solution(
                journal.run_id, sim_cfg.threshold, candidates, must, cannot
            )
            journal.succeed(
                clusters=len(solution.clusters),
                changed_records=changed,
                method=result.method,
            )
            return solution
        except EntityResolutionError as exc:
            journal.fail(exc.category, exc.code.value, exc.message, exc.details)
            raise
        finally:
            journal.flush()

    def _locked_id_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for cluster in self.storage.list_clusters():
            if cluster["locked"]:
                for member in cluster["members"]:
                    out[member] = cluster["cluster_id"]
        return out

    def _next_auto_index(self) -> int:
        """Monotonic base so auto cluster ids are never reused across runs."""
        return self.storage.max_auto_suffix()

    def _previous_unlocked_by_members(self) -> dict[frozenset[str], str]:
        """Map member-set -> cluster id for current unlocked clusters."""
        out: dict[frozenset[str], str] = {}
        for cluster in self.storage.list_clusters():
            if not cluster["locked"]:
                out[frozenset(cluster["members"])] = cluster["cluster_id"]
        return out

    def _plan_cluster_ids(
        self,
        result,
        locked_id_by_member: dict[str, str],
        previous_by_members: dict[frozenset[str], str],
        base_index: int = 0,
    ) -> list[tuple[str, list[str], bool]]:
        """Assign output cluster ids deterministically.

        Identity preservation rules (so the change report stays meaningful):

        * a result cluster containing a locked block keeps that locked id for
          the whole group (its unlocked neighbors may join it);
        * an *unlocked* cluster whose member set is unchanged reuses its
          previous id, so stable groups are not renamed run after run;
        * genuinely new groups receive never-before-used ``cNNNN`` ids.
        """
        plan: list[tuple[str, list[str], bool]] = []
        locked_ids = set(locked_id_by_member.values())
        next_index = base_index

        def next_auto_id() -> str:
            nonlocal next_index
            cid = _cluster_id(next_index)
            while cid in locked_ids or cid in set(previous_by_members.values()):
                next_index += 1
                cid = _cluster_id(next_index)
            next_index += 1
            return cid

        for members in sorted(result.clusters, key=lambda c: min(c)):
            member_list = sorted(members)
            member_set = frozenset(member_list)
            member_locked = {
                locked_id_by_member[m]
                for m in member_list
                if m in locked_id_by_member
            }
            # At most one distinct locked id can land in a result cluster:
            # solve() injects cross-lock cannot-links, so this is guaranteed.
            if member_locked:
                cid = sorted(member_locked)[0]
                locked = True
            elif member_set in previous_by_members:
                cid = previous_by_members[member_set]
                locked = False
            else:
                cid = next_auto_id()
                locked = False
            plan.append((cid, member_list, locked))
        return plan

    def _build_solution(
        self, run_id, threshold, candidates, must, cannot
    ) -> ClusterSolution:
        records_by_id = {r["id"]: r for r in self.storage.list_records()}
        # evidence lookup by canonical pair
        evidence_by_pair = {
            tuple(sorted((ps.left, ps.right))): ps for ps in candidates.values()
        }

        # Persisted clusters are the single source of truth for ids/membership.
        persisted = self.storage.list_clusters()
        clustered_together: set[tuple[str, str]] = set()
        clusters_out: list[ClusterOut] = []
        for cluster in sorted(persisted, key=lambda c: c["cluster_id"]):
            member_list = sorted(cluster["members"])
            for i, a in enumerate(member_list):
                for b in member_list[i + 1 :]:
                    clustered_together.add(tuple(sorted((a, b))))
            ev: list[PairEvidence] = []
            for i, a in enumerate(member_list):
                for b in member_list[i + 1 :]:
                    ps = evidence_by_pair.get(tuple(sorted((a, b))))
                    if ps is not None:
                        ev.append(PairEvidence(**ps.as_evidence()))
            canonical_name = self._canonical_display(member_list, records_by_id)
            clusters_out.append(
                ClusterOut(
                    cluster_id=cluster["cluster_id"],
                    members=member_list,
                    locked=cluster["locked"],
                    canonical_name=canonical_name,
                    evidence=ev,
                )
            )

        rejected: list[dict] = []
        for (a, b), ps in sorted(candidates.items()):
            if (a, b) in clustered_together:
                continue
            if (a, b) in cannot:
                reason = "cannot_link"
            elif ps.vetoed:
                reason = "hard_attribute_conflict"
            elif ps.candidate:
                reason = "global_objective_split"
            else:
                # Pairs that never reached candidate strength are routine
                # non-matches; omit them to keep provenance signal-dense.
                continue
            rejected.append(
                {"pair": [a, b], "score": round(ps.score, 4), "reason": reason}
            )

        return ClusterSolution(
            run_id=run_id,
            threshold=threshold,
            clusters=clusters_out,
            rejected_pairs=rejected,
            must_links=[list(p) for p in sorted(must)],
            cannot_links=[list(p) for p in sorted(cannot)],
        )

    @staticmethod
    def _canonical_display(member_ids: list[str], records_by_id: dict) -> str:
        # Deterministic: shortest canonicalized name, tie -> lexicographic.
        from .normalization import normalize_name

        def key(rid: str) -> tuple[int, str]:
            nn = normalize_name(records_by_id[rid]["name"])
            return (len(nn.canonical), nn.canonical)

        chosen = min(member_ids, key=key)
        return records_by_id[chosen]["name"]

    # ------------------------------------------------------------- impact API

    def affected_entities(self, run_id: str | None = None) -> AffectedEntities:
        """Compute change impact from the most recent (or given) run's audit."""
        events = self.storage.list_audit(limit=10_000)
        if run_id is not None:
            events = [e for e in events if e["run_id"] == run_id]
        if not events:
            raise NoSuchClusterError(
                "no changes recorded for that run", {"run_id": run_id}
            )
        # list_audit returns newest first; take the contiguous latest run.
        latest_run = events[0]["run_id"]
        events = [e for e in events if e["run_id"] == latest_run]
        changed = sorted({e["record_id"] for e in events if e["record_id"]})
        before = {e["record_id"]: e["before"] for e in events}
        after = {e["record_id"]: e["after"] for e in events}
        before_clusters = {v for v in before.values() if v}
        after_clusters = {v for v in after.values() if v}
        return AffectedEntities(
            run_id=latest_run,
            changed_records=changed,
            clusters_created=sorted(after_clusters - before_clusters),
            clusters_dissolved=sorted(before_clusters - after_clusters),
            clusters_modified=sorted(before_clusters & after_clusters),
            before=before,
            after=after,
        )

    # ------------------------------------------------------------- links/locks

    def add_link(self, link: LinkIn) -> LinkResult:
        journal = self._journal("link")
        try:
            pair = canon_pair(link.left, link.right)
            # endpoint existence
            for endpoint in pair:
                self.storage.get_record(endpoint)
            must, cannot = self.storage.list_links()
            locked = self.storage.locked_blocks()
            new_must = set(must) | ({pair} if link.kind.value == "must" else set())
            new_cannot = set(cannot) | ({pair} if link.kind.value == "cannot" else set())
            from .clustering import validate_constraints

            ids = [r["id"] for r in self.storage.list_records()]
            validate_constraints(ids, new_must, new_cannot, locked)
            added = self.storage.add_link(pair[0], pair[1], link.kind.value)
            journal.decide(
                f"{pair[0]}~{pair[1]}",
                f"constraint_{link.kind.value}_accepted",
                added=added,
            )
            journal.succeed(added=added)
            return LinkResult(
                left=pair[0], right=pair[1], kind=link.kind, added=added
            )
        except EntityResolutionError as exc:
            journal.fail(exc.category, exc.code.value, exc.message, exc.details)
            raise
        finally:
            journal.flush()

    def lock_cluster(self, cluster_id: str, members: list[str] | None,
                     expected_version: int | None = None) -> dict:
        journal = self._journal("lock")
        try:
            ids = [r["id"] for r in self.storage.list_records()]
            chosen = sorted(set(members)) if members else None
            if chosen is None:
                current = self.storage.get_cluster(cluster_id)
                if current is None:
                    raise NoSuchClusterError(
                        "unknown cluster and no members provided",
                        {"cluster_id": cluster_id},
                    )
                chosen = current["members"]
            for member in chosen:
                if member not in ids:
                    raise InvalidRequestError(
                        "lock references unknown record", {"record": member}
                    )
            # Locking asserts a must-link clique that must not contradict cannot.
            must_pairs = {
                tuple(sorted((a, b)))
                for i, a in enumerate(chosen)
                for b in chosen[i + 1 :]
            }
            existing_must, existing_cannot = self.storage.list_links()
            existing_locked = self.storage.locked_blocks()
            from .clustering import validate_constraints

            validate_constraints(
                ids, existing_must | must_pairs, existing_cannot, existing_locked
            )
            cluster = self.storage.lock_cluster(
                cluster_id, chosen, expected_version, run_id=journal.run_id
            )
            journal.decide(
                cluster_id, "cluster_locked", members=chosen, version=cluster["version"]
            )
            journal.succeed(cluster_id=cluster_id, members=len(chosen))
            return cluster
        except EntityResolutionError as exc:
            journal.fail(exc.category, exc.code.value, exc.message, exc.details)
            raise
        finally:
            journal.flush()

    # ------------------------------------------------------------- reads

    def list_records(self) -> list[RecordOut]:
        assignment = self.storage.assignment()
        locked_members = set(self._locked_id_map())
        out = []
        for row in self.storage.list_records():
            out.append(
                RecordOut(
                    id=row["id"],
                    name=row["name"],
                    language=row["language"],
                    attributes=row["attributes"],
                    cluster_id=assignment.get(row["id"]),
                    locked=row["id"] in locked_members,
                    version=row["version"],
                    created_at=row["created_at"],
                )
            )
        return out
