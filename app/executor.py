"""Drain executor: turns a predicted plan into transitions on the simulator.

The executor never speaks to a real cluster — it only mutates a
:class:`~app.simulator.Simulator`.  Before every wave it re-evaluates
preconditions against the *live* snapshot and builds the wave with
:func:"app.planner.next_wave", so:

* a snapshot generation change (out-of-band mutation) is handled by simply
  rebuilding the next wave from the new snapshot — "generation change ⇒
  recompute";
* a deleted pod is never treated as a ready replacement: the next wave waits
  until earlier evictions have been garbage-collected and their replacements
  are Ready;
* cancelling leaves the node cordoned and in-flight evictions in place: the
  workload keeps healing in the simulator, but no more evictions are issued.

Plan tokens are HMAC-signed and bind (drain, plan generation, step, tick);
a stale generation or step invalidates them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .eviction import admit_evictions, hard_eviction_blocker
from .models import (
    CordonStep,
    CompleteStep,
    DrainState,
    EvictWaveStep,
    Event,
    Plan,
    Precondition,
)
from .planner import (
    PlanRequest,
    build_plan,
    next_wave,
    predicted_pdbs,
    target_evictable_pods,
)
from .security import sign_token, verify_token
from .simulator import Simulator


class ExecutorError(Exception):
    def __init__(self, message: str, code: str = "executor_error", status: int = 409,
                 fresh_token: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.fresh_token = fresh_token


# --------------------------------------------------------------------------- #
@dataclass
class Drain:
    drain_id: str
    sim: Simulator
    plan: Plan
    secret: str
    state: DrainState = DrainState.PLANNED
    cordoned_done: set[str] = field(default_factory=set)
    evicted: set[str] = field(default_factory=set)   # pod uid (fallback name)
    wave_count: int = 0
    current_wave: Optional[EvictWaveStep] = None
    cancel_requested: bool = False
    events: list[Event] = field(default_factory=list)
    ticks_consumed: int = 0
    # Out-of-band generation watermark observed when the current token was
    # minted. The drain's own steps/ticks do not move this watermark; only an
    # explicit external mutation does, and that invalidates an older token.
    observed_external: int = 0

    # ------------------------------------------------------------------ #
    def log(self, type_: str, detail: str) -> None:
        self.events.append(Event(
            tick=self.sim.snap.tick,
            generation=self.sim.snap.generation,
            type=type_,
            detail=detail,
        ))

    @property
    def pending_cordon(self) -> Optional[CordonStep]:
        for node in self.plan.nodes:
            if node not in self.cordoned_done:
                return CordonStep(node=node)
        return None

    @property
    def complete_step(self) -> CompleteStep:
        return next(
            s for s in self.plan.steps if isinstance(s, CompleteStep)
        )

    @property
    def current_step_kind(self) -> str:
        if self.cancel_requested:
            return "Cancelled"
        if self.pending_cordon is not None:
            return "Cordon"
        if self.current_wave is not None:
            return "EvictWave"
        return "Complete"

    def step_index(self) -> int:
        if self.pending_cordon is not None:
            return self.plan.nodes.index(self.pending_cordon.node)
        # Once cordons are done the cursor is numCordons + (waves already
        # evicted). The wave at that exact cursor is computed on demand.
        return len(self.plan.nodes) + self.wave_count

    def current_step(self):
        if self.pending_cordon is not None:
            return self.pending_cordon
        step = _current_wave_step(self)
        if step is not None:
            return step
        if self.state != DrainState.COMPLETE:
            return self.complete_step
        return None

    def token(self) -> str:
        """HMAC token binding drain + out-of-band watermark + step cursor.

        The token records the snapshot's ``external_generation`` watermark and
        the current step index. The drain's own evictions/cordons/ticks bump
        the ordinary generation but NOT this watermark, so consecutive tokens
        chain correctly. Only an explicit out-of-band mutation raises the
        watermark and invalidates an older token (``stale_generation``). The
        step cursor prevents replay of an already-spent token.
        """
        return sign_token(self.secret, {
            "drain": self.drain_id,
            "egen": self.sim.external_generation,
            "step": self.step_index(),
            "tick": self.sim.snap.tick,
        })


@dataclass
class StepResult:
    executed: bool
    state: str
    current_step: Optional[dict]
    step_index: int
    preconditions: list[Precondition]
    events: list[Event]
    token: str
    ticks_used: int = 0
    replanned: bool = False
    message: str = ""


# --------------------------------------------------------------------------- #
def create_drain(
    sim: Simulator,
    drain_id: str,
    secret: str,
    nodes: list[str],
    ignore_daemon_sets: bool = True,
    uncordon_on_complete: bool = False,
) -> Drain:
    req = PlanRequest(
        drain_id=drain_id, nodes=list(nodes),
        ignore_daemon_sets=ignore_daemon_sets,
        uncordon_on_complete=uncordon_on_complete,
    )
    plan = build_plan(sim.public_snapshot(), req)
    drain = Drain(drain_id=drain_id, sim=sim, plan=plan, secret=secret)
    drain.observed_external = sim.external_generation
    drain.log(
        "plan",
        f"predicted plan at generation {plan.generation} with "
        f"{sum(1 for s in plan.steps if s.kind == 'EvictWave')} wave(s)",
    )
    if plan.blockers:
        drain.state = DrainState.BLOCKED
        drain.log("blocked", f"{len(plan.blockers)} blocker(s) identified before start")
    return drain


def _refresh_plan_generation(drain: Drain) -> bool:
    """Re-predict against the live snapshot when the generation moved."""
    live_gen = drain.sim.snap.generation
    if live_gen == drain.plan.generation:
        return False
    req = PlanRequest(
        drain_id=drain.drain_id,
        nodes=list(drain.plan.nodes),
        ignore_daemon_sets=drain.plan.ignore_daemon_sets,
        uncordon_on_complete=drain.complete_step.uncordon,
    )
    old = drain.plan.generation
    drain.plan = build_plan(drain.sim.public_snapshot(), req)
    drain.log("replan", f"plan recomputed at generation {live_gen} (was {old})")
    return True


def _compute_next_wave(drain: Drain) -> tuple[list, list]:
    pods, blockers, notes = next_wave(
        drain.sim.snap,
        drain.plan.nodes,
        drain.plan.ignore_daemon_sets,
        wave_index=drain.wave_count,
        already_evicted=drain.evicted,
        include_waiting=True,
    )
    for note in notes:
        drain.log("wave-note", note)
    return pods, blockers


def _hard_pod_blockers(drain: Drain) -> list:
    """Mirror/static pods still present on the drained nodes."""
    nodes = set(drain.plan.nodes)
    out = []
    for p in drain.sim.snap.pods:
        if p.node not in nodes or p.deleting:
            continue
        reason = hard_eviction_blocker(p)
        if reason is not None:
            from .models import Blocker
            out.append(Blocker(node=p.node, pod=p.name,
                               namespace=p.namespace, reason=reason))
    return out


def _current_wave_step(drain: Drain) -> Optional[EvictWaveStep]:
    """Recompute the wave at the current cursor against the live snapshot.

    Returns None when no evictable pod remains. This is deliberately not
    cached: the set of admissible pods can change as replacements mature or an
    out-of-band change lands, and ``step_index`` stays stable within a wave.
    """
    pods, _ = _compute_next_wave(drain)
    if not pods:
        return None
    drain.current_wave = EvictWaveStep(
        index=drain.wave_count,
        pods=[p.name for p in pods],
        predictedPdbs=predicted_pdbs(drain.sim.snap),
    )
    return drain.current_wave


# --------------------------------------------------------------------------- #
# Preconditions — every step emits the conditions its execution depends on.
# --------------------------------------------------------------------------- #
def _preconditions_cordon(drain: Drain, step: CordonStep) -> list[Precondition]:
    node = next((n for n in drain.sim.snap.nodes if n.name == step.node), None)
    return [Precondition(
        id="node-exists",
        description=f"node {step.node} exists in the snapshot",
        satisfied=node is not None,
        detail="missing node" if node is None else f"generation {drain.plan.generation}",
    )]


def _pods_by_name(drain: Drain, names: list[str]):
    return {p.name: p for p in drain.sim.snap.pods if p.name in names}


def _preconditions_wave(drain: Drain, step: EvictWaveStep) -> list[Precondition]:
    snap = drain.sim.snap
    pre: list[Precondition] = []

    pre.append(Precondition(
        id="snapshot-generation",
        description="no out-of-band change has landed since the wave was built",
        satisfied=drain.sim.external_generation <= drain.observed_external,
        detail=(
            f"external watermark={drain.sim.external_generation} "
            f"observed={drain.observed_external}; snapshot generation="
            f"{snap.generation}"
        ),
        severity="soft",
    ))

    by_name = _pods_by_name(drain, step.pods)
    missing = [n for n in step.pods if n not in by_name]
    pre.append(Precondition(
        id="pods-present-on-drain-set",
        description="every pod of this wave still exists on a drained node",
        satisfied=not missing,
        detail=("missing: " + ", ".join(missing)) if missing
        else f"{len(step.pods)} pod(s) present",
    ))

    terminating_now = [n for n, p in by_name.items() if p.deleting]
    pre.append(Precondition(
        id="pods-not-terminating",
        description="no pod of this wave is already terminating",
        satisfied=not terminating_now,
        detail=("terminating: " + ", ".join(terminating_now))
        if terminating_now else "none terminating",
    ))

    # Barrier A: previously evicted pods must have physically disappeared.
    cluster_terminating = [
        f"{p.namespace}/{p.name}" for p in snap.pods if p.deleting
    ]
    pre.append(Precondition(
        id="prior-evictions-garbage-collected",
        description="previously evicted pods have been removed (no deletionTimestamp)",
        satisfied=not cluster_terminating,
        detail=("awaiting GC: " + ", ".join(cluster_terminating[:4]))
        if cluster_terminating else "no terminating pods",
    ))

    # Barrier B: every replacement created by earlier waves must be Ready.
    pending_replacements = [
        f"{p.namespace}/{p.name}"
        for p in snap.pods
        if p.origin == "replacement" and (not p.ready or p.phase != "Running")
    ]
    pre.append(Precondition(
        id="earlier-replacements-ready",
        description="replacement pods of earlier waves are Ready",
        satisfied=not pending_replacements,
        detail=("not ready: " + ", ".join(pending_replacements[:4]))
        if pending_replacements else "all replacements Ready",
    ))

    if not missing and not terminating_now:
        live = [by_name[n] for n in step.pods]
        admitted, rejected = admit_evictions(snap, live)
        rejected_names = {r.pod for r in rejected}
        pre.append(Precondition(
            id="multi-pdb-intersection",
            description="each pod is admitted simultaneously by every selecting PDB",
            satisfied=not rejected_names,
            detail=("rejected: " + "; ".join(
                f"{r.pod} [{r.pdb or '-'}] {r.reason}" for r in rejected
            )) if rejected_names else f"{len(admitted)} pod(s) admitted by all PDBs",
        ))
    return pre


def _preconditions_complete(drain: Drain) -> list[Precondition]:
    remaining, blockers = target_evictable_pods(
        drain.sim.snap, drain.plan.nodes, drain.plan.ignore_daemon_sets
    )
    pre = [Precondition(
        id="drain-set-empty-of-evictable-pods",
        description="no evictable, non-daemonset pods remain on the drained nodes",
        satisfied=not remaining,
        detail=("remaining: " + ", ".join(
            f"{p.namespace}/{p.name}" for p in remaining[:6]
        )) if remaining else "drain set empty",
    )]
    pre.append(Precondition(
        id="no-hard-blockers",
        description="no mirror/static pod or unschedulable replacement blocks the drain",
        satisfied=not blockers,
        detail="; ".join(f"{b.namespace}/{b.pod}: {b.reason}" for b in blockers[:4])
        if blockers else "none",
    ))
    pre.append(Precondition(
        id="in-flight-work-settled",
        description="no terminating pods and no not-ready replacements remain",
        satisfied=not any(p.deleting for p in drain.sim.snap.pods)
        and not any(
            p.origin == "replacement" and (not p.ready or p.phase != "Running")
            for p in drain.sim.snap.pods
        ),
        detail="waiting for evictions/replacements to settle",
    ))
    return pre


def evaluate(drain: Drain) -> list[Precondition]:
    if drain.state == DrainState.CANCELLED:
        return []
    cordon = drain.pending_cordon
    if cordon is not None:
        return _preconditions_cordon(drain, cordon)
    if drain.current_wave is not None:
        return _preconditions_wave(drain, drain.current_wave)
    return _preconditions_complete(drain)


def _all_hard_satisfied(pre: list[Precondition]) -> bool:
    return all(p.satisfied for p in pre if p.severity == "hard")


# --------------------------------------------------------------------------- #
def _check_token(drain: Drain, token: str) -> None:
    """Validate signature, drain ownership and the out-of-band watermark.

    Used for non-step actions (cancel) where the step cursor is irrelevant.
    """
    try:
        payload = verify_token(drain.secret, token)
    except Exception as exc:  # noqa: BLE001
        raise ExecutorError(f"invalid plan token: {exc}",
                            code="invalid_token", status=401)
    if payload.get("drain") != drain.drain_id:
        raise ExecutorError("token belongs to a different drain",
                            code="invalid_token", status=401)
    if payload.get("egen", drain.sim.external_generation) < \
            drain.sim.external_generation:
        raise ExecutorError(
            "stale token: an out-of-band change occurred after this token was "
            "minted; re-fetch a token",
            code="stale_generation", status=409, fresh_token=drain.token(),
        )


# --------------------------------------------------------------------------- #
# Execution primitives
# --------------------------------------------------------------------------- #
def _execute_cordon(drain: Drain, step: CordonStep) -> None:
    drain.sim.cordon(step.node)
    drain.cordoned_done.add(step.node)
    drain.log("execute", f"cordoned node {step.node}")


def _execute_wave(drain: Drain, step: EvictWaveStep) -> None:
    by_name = _pods_by_name(drain, step.pods)
    live = [by_name[n] for n in step.pods if n in by_name]
    admitted, rejected = admit_evictions(drain.sim.snap, live)
    if rejected or len(admitted) != len(live):
        raise ExecutorError(
            "admission changed between precondition check and execution: "
            + ", ".join(r.pod for r in rejected),
            code="admission_lost", fresh_token=drain.token(),
        )
    for pod in admitted:
        drain.sim.evict(pod, drain_id=drain.drain_id)
        drain.evicted.add(pod.uid or pod.name)
    drain.log("execute", f"evicted wave {step.index}: {', '.join(step.pods)}")
    drain.wave_count += 1
    drain.current_wave = None


def _execute_complete(drain: Drain) -> None:
    step = drain.complete_step
    if step.uncordon:
        for node in step.nodes:
            drain.sim.uncordon(node)
        drain.log("execute", f"uncordoned {', '.join(step.nodes)}")
    drain.state = DrainState.COMPLETE
    drain.log("complete", f"drain {drain.drain_id} complete")


# --------------------------------------------------------------------------- #
def advance(drain: Drain, token: str, tick_wait: int = 0) -> StepResult:
    """Execute exactly one step when its hard preconditions all hold.

    Ticks the simulated clock up to ``tick_wait`` times first, stopping early
    as soon as the conditions hold. Nothing is executed while a hard
    precondition is unsatisfied; the reported preconditions explain why.
    """
    if drain.cancel_requested or drain.state == DrainState.CANCELLED:
        drain.state = DrainState.CANCELLED
        raise ExecutorError("drain was cancelled", code="cancelled", status=409)

    # --- Validate token and reconcile generation ---------------------------
    # The token binds the out-of-band watermark + step cursor. The drain's
    # own steps/ticks bump the ordinary generation but never the watermark, so
    # they do not invalidate the token chain. An explicit external mutation
    # raises the watermark ("generation change ⇒ recompute") and rejects an
    # older token so the client must re-fetch the recomputed plan.
    try:
        payload = verify_token(drain.secret, token)
    except Exception as exc:  # noqa: BLE001
        raise ExecutorError(f"invalid plan token: {exc}",
                            code="invalid_token", status=401)
    if payload.get("drain") != drain.drain_id:
        raise ExecutorError("token belongs to a different drain",
                            code="invalid_token", status=401)

    live_external = drain.sim.external_generation
    token_external = payload.get("egen", live_external)
    gen_moved = _refresh_plan_generation(drain)
    drain.observed_external = live_external

    if payload.get("step") != drain.step_index():
        raise ExecutorError(
            f"stale token: token step {payload.get('step')} != current step "
            f"{drain.step_index()}", code="stale_step", status=409,
            fresh_token=drain.token(),
        )
    if token_external < live_external:
        raise ExecutorError(
            f"stale token: an out-of-band change advanced the snapshot "
            f"watermark to {live_external} (token knew {token_external}); the "
            f"plan was recomputed — re-fetch a token",
            code="stale_generation", status=409, fresh_token=drain.token(),
        )

    # Step type 1: cordon.
    cordon = drain.pending_cordon
    if cordon is not None:
        pre = _preconditions_cordon(drain, cordon)
        if not _all_hard_satisfied(pre):
            drain.state = DrainState.WAITING
            return _waiting(drain, pre, 0, gen_moved, "cordon preconditions unmet")
        _execute_cordon(drain, cordon)
        drain.state = DrainState.READY
        return _done(drain, pre, 0, gen_moved, f"cordoned {cordon.node}")

    # Step type 2: eviction wave at the current cursor. The wave is computed
    # from the live snapshot every call; waiting for its barriers may consume
    # clock ticks.
    pods, blockers = _compute_next_wave(drain)
    capacity_blockers = [b for b in blockers if "no schedulable node" in b.reason]
    hard_pod_blockers = _hard_pod_blockers(drain)
    # A permanent hard blocker (mirror/static pod, or nowhere to reschedule)
    # means the drain set can never empty: report BLOCKED like `kubectl drain`
    # rather than WAITING, regardless of whether other pods could still move.
    permanent = hard_pod_blockers or capacity_blockers
    if permanent:
        drain.state = DrainState.BLOCKED
        raise ExecutorError(
            "drain is BLOCKED: "
            + "; ".join(f"{b.namespace}/{b.pod} on {b.node} ({b.reason})"
                        for b in permanent[:5]),
            code="blocked", fresh_token=drain.token(),
        )
    step = _current_wave_step(drain)
    if step is not None:
        pre = _preconditions_wave(drain, step)
        ticks_used = 0
        while not _all_hard_satisfied(pre) and ticks_used < tick_wait:
            drain.sim.tick(1)
            ticks_used += 1
            drain.ticks_consumed += 1
            fresh = _current_wave_step(drain)
            if fresh is not None and fresh.pods != step.pods:
                step = fresh
            pre = _preconditions_wave(drain, step)
        if not _all_hard_satisfied(pre):
            drain.state = DrainState.WAITING
            return _waiting(
                drain, pre, ticks_used, gen_moved,
                f"wave {step.index} preconditions not satisfied after "
                f"{ticks_used} tick(s)",
            )
        _execute_wave(drain, step)
        drain.state = DrainState.READY
        return _done(drain, pre, ticks_used, gen_moved,
                     f"wave {step.index} evicted {len(step.pods)} pod(s)")

    # Step type 3: completion.
    pre = _preconditions_complete(drain)
    ticks_used = 0
    while not _all_hard_satisfied(pre) and ticks_used < tick_wait:
        drain.sim.tick(1)
        ticks_used += 1
        drain.ticks_consumed += 1
        pre = _preconditions_complete(drain)
    if not _all_hard_satisfied(pre):
        drain.state = DrainState.WAITING
        return _waiting(drain, pre, ticks_used, gen_moved,
                        "completion preconditions not satisfied")
    _execute_complete(drain)
    return _done(drain, pre, ticks_used, gen_moved, "drain complete")


def _waiting(drain, pre, ticks, replanned, message) -> StepResult:
    return StepResult(
        executed=False, state=drain.state.value,
        current_step=_step_dump(drain), step_index=drain.step_index(),
        preconditions=pre, events=list(drain.events), token=drain.token(),
        ticks_used=ticks, replanned=replanned, message=message,
    )


def _done(drain, pre, ticks, replanned, message) -> StepResult:
    return StepResult(
        executed=True, state=drain.state.value,
        current_step=_step_dump(drain), step_index=drain.step_index(),
        preconditions=pre, events=list(drain.events), token=drain.token(),
        ticks_used=ticks, replanned=replanned, message=message,
    )


def _step_dump(drain: Drain) -> Optional[dict]:
    step = drain.current_step()
    return step.model_dump(by_alias=True, mode="json") if step else None


def autorun(drain: Drain, token: str, max_ticks: int = 60) -> dict:
    """Drive the drain forward with a global tick budget.

    The first step that cannot make progress (e.g. a replacement that never
    becomes ready) stops the run in WAITING without violating any constraint.
    """
    history: list[dict] = []
    budget = max_ticks
    guard = 0
    while guard < 10_000:
        guard += 1
        result = advance(drain, token, tick_wait=budget)
        token = result.token
        budget -= result.ticks_used
        history.append({
            "executed": result.executed,
            "state": result.state,
            "stepIndex": result.step_index,
            "ticksUsed": result.ticks_used,
            "replanned": result.replanned,
            "message": result.message,
            "currentStep": result.current_step,
        })
        if result.state in ("COMPLETE", "BLOCKED", "CANCELLED"):
            break
        if not result.executed:
            # WAITING: the live world does not satisfy the step and no waiting
            # budget remained. Nothing unsafe was done.
            break
    return {
        "finalState": drain.state.value,
        "history": history,
        "ticksConsumed": drain.ticks_consumed,
        "token": token,
        "blockers": [b.model_dump(by_alias=True) for b in drain.plan.blockers],
    }


def request_cancel(drain: Drain, token: str) -> None:
    _check_token(drain, token)
    if drain.state == DrainState.COMPLETE:
        raise ExecutorError("drain already complete", code="already_complete")
    drain.cancel_requested = True
    drain.state = DrainState.CANCELLED
    drain.log(
        "cancel",
        "cancellation requested: no further evictions will be issued; nodes "
        "stay cordoned and in-flight replacements continue to mature",
    )
