"""Tests for the discrete-event scheduling reference and the RTA<->sim agreement."""

from app.models import Task
from app.simulation import hyperperiod, simulate
from app.rta import run_rta


def mk(id, c, t, d, p, b=0):
    return Task(id=id, wcet=c, period=t, deadline=d, blocking=b, priority=p)


class TestHyperperiod:
    def test_lcm(self):
        assert hyperperiod([mk("a", 1, 4, 4, 1), mk("b", 1, 6, 6, 2)]) == 12
        assert hyperperiod([mk("a", 1, 3, 3, 1), mk("b", 1, 7, 7, 2)]) == 21


class TestHandBuiltSchedule:
    def test_two_task_timeline(self):
        # T1: C1 T4  (hp); T2: C2 T7
        # t=0 T1 runs [0,1); T2 runs [1,3) -> T2 first response 3.
        t1, t2 = mk("T1", 1, 4, 4, 1), mk("T2", 2, 7, 7, 2)
        out = simulate([t1, t2])
        assert out.first_job_response_times == {"T1": 1, "T2": 3}
        assert out.deadline_misses == 0

    def test_preemption_timeline(self):
        # T1: C2 T6 (hp); T2: C5 T20.  T2's first job is preempted once by the
        # T1 release at t=6.  Anchor the expectation to the RTA fixed point and
        # check the preemptive timeline is strictly longer than no-interference.
        t1, t2 = mk("T1", 2, 6, 6, 1), mk("T2", 5, 20, 20, 2)
        from app.rta import rta_for_task

        wcrt = rta_for_task(t2, [t1, t2]).response_time
        # R0=5 -> R1=5+ceil(5/6)*2=7 -> R2=7+ceil(7/6)*2=9 fixed
        assert wcrt == 9
        out = simulate([t1, t2])
        assert out.first_job_response_times["T1"] == 2
        assert out.first_job_response_times["T2"] == wcrt
        assert out.first_job_response_times["T2"] > t2.wcet  # preemption delayed it
        assert out.deadline_misses == 0

    def test_deadline_miss_observed(self):
        # T2 constrained deadline D=2 gets preempted; first job finishes at 3.
        t1, t2 = mk("T1", 1, 3, 3, 1), mk("T2", 2, 7, 2, 2)
        out = simulate([t1, t2])
        assert out.first_job_response_times["T2"] == 3
        assert out.deadline_misses >= 1
        assert out.first_miss is not None
        assert out.first_miss["task_id"] == "T2"
        assert out.first_miss["deadline"] == 2

    def test_blocking_chunk_delays_target(self):
        # T1 C2 T10, T2 C2 T5 with B2=3: non-preemptible [0,3); T2 then needs
        # 2 units, but T1 (hp) released at 0 was also held until 3 and runs
        # first [3,5); T2 completes at 7.
        t1, t2 = mk("T1", 2, 10, 10, 1), mk("T2", 2, 5, 5, 2, b=3)
        out = simulate([t1, t2], blocking_override={"T2": 3})
        assert out.first_job_response_times["T2"] == 7


class TestRtaSimulationAgreement:
    def test_agreement_on_schedulable_sets(self, analyze, schedulable_payload):
        res = analyze(schedulable_payload)
        assert res["schedulable"] is True
        assert res["simulation"]["crosscheck"] == "agree"
        for t in res["tasks"]:
            assert t["rta_crosscheck"] == "agree"
            # Baseline zero-blocking timeline equals RTA exactly for B=0 tasks;
            # B>0 tasks are instead validated by the blocking-aware simulation.
            if t["blocking"] == 0:
                assert t["response_time"] == res["simulation"]["first_job_response_times"][t["id"]]

    def test_blocking_task_response_exceeds_zero_blocking_baseline(
        self, analyze, schedulable_payload
    ):
        res = analyze(schedulable_payload)
        t2 = next(t for t in res["tasks"] if t["id"] == "T2")
        # B=1 adds one unit over the pure preemptive baseline (3 -> 4).
        assert t2["response_time"] == 4
        assert res["simulation"]["first_job_response_times"]["T2"] == 3
        assert t2["rta_crosscheck"] == "agree"

    def test_agreement_on_low_u_failure(self, analyze, low_u_unschedulable_payload):
        res = analyze(low_u_unschedulable_payload)
        assert res["schedulable"] is False
        assert res["simulation"]["crosscheck"] == "agree"
        t2 = next(t for t in res["tasks"] if t["id"] == "T2")
        assert t2["continued_fixed_point"] == 3
        assert t2["rta_crosscheck"] == "agree"
        assert res["simulation"]["deadline_misses"] >= 1

    def test_agreement_on_blocking_failure(self, analyze, blocking_miss_payload):
        res = analyze(blocking_miss_payload)
        assert res["schedulable"] is False
        assert res["simulation"]["crosscheck"] == "agree"

    def test_simulation_disable(self, analyze, schedulable_payload):
        schedulable_payload["options"] = {"run_simulation": False}
        res = analyze(schedulable_payload)
        assert res["simulation"]["enabled"] is False
        assert res["simulation"]["crosscheck"] == "skipped"
        assert all(t["rta_crosscheck"] == "skipped" for t in res["tasks"])


class TestDirectRtaVsSim:
    def test_many_random_sets_agree(self):
        """Property-style check: for small generated task sets that converge,
        the simulated critical-instant response equals the RTA fixed point."""
        import random

        rng = random.Random(20260923)
        checked = 0
        for _ in range(60):
            n = rng.randint(1, 4)
            prios = rng.sample(range(1, 20), n)
            tasks = []
            for i, p in enumerate(prios):
                period = rng.randint(4, 12)
                wcet = rng.randint(1, max(1, period // 3))
                d = rng.randint(wcet, period)  # constrained
                b = rng.choice([0, 0, 0, rng.randint(0, 2)])
                tasks.append(mk(f"T{i}", wcet, period, d, p, b=b))
            rta_results = run_rta(tasks)
            baseline = simulate(tasks, blocking_override={})
            for r in rta_results:
                target = (
                    r.response_time if r.response_time is not None else r.continued_fixed_point
                )
                if target is None:
                    continue
                task = next(t for t in tasks if t.id == r.task_id)
                if task.blocking:
                    out = simulate(
                        tasks,
                        blocking_override={task.id: task.blocking},
                    )
                else:
                    out = baseline
                observed = out.first_job_response_times.get(r.task_id)
                if observed is not None:
                    assert observed == target, (
                        f"mismatch for {task.id}: sim={observed} rta={target} "
                        f"set={[(x.wcet,x.period,x.deadline,x.blocking,x.priority) for x in tasks]}"
                    )
                    checked += 1
        assert checked >= 30
