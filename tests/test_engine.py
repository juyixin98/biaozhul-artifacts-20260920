"""Tests for the time-ordered fusion engine (replay, gating, covariance)."""

import numpy as np
import pytest

from ekf_fusion.engine import DEFAULT_GATE, LATE_WINDOW_S, FusionEngine

R_G = [[0.04, 0.0], [0.0, 0.04]]
R_O = [[0.0025, 0.0], [0.0, 0.0025]]
V = np.array([1.0, 0.4])


def truth_gnss(t, rng=None, scale=0.0):
    p = V * t
    if rng is not None:
        p = p + rng.normal(scale=scale, size=2)
    return p


def make_stream(t_max=10.0, odom_dt=0.1, gnss_dt=0.2, seed=1, noise=False):
    rng = np.random.default_rng(seed) if noise else None
    out = []
    i = 0
    for t in np.arange(0.02, t_max, odom_dt):
        z = V + (rng.normal(scale=0.05, size=2) if noise else 0.0)
        out.append(dict(mid=f"o{i}", t=round(float(t), 6), kind="odom",
                        z=np.asarray(z, dtype=float), r=np.array(R_O)))
        i += 1
    for t in np.arange(0.1, t_max, gnss_dt):
        z = truth_gnss(float(t), rng, 0.15)
        out.append(dict(mid=f"g{i}", t=round(float(t), 6), kind="gnss",
                        z=np.asarray(z, dtype=float), r=np.array(R_G)))
        i += 1
    return out


def feed(eng: FusionEngine, msgs: list[dict]):
    return [eng.submit(m["mid"], m["t"], m["kind"], m["z"], m["r"])
            for m in msgs]


def test_ordered_and_out_of_order_identical():
    """Core acceptance: shuffled bounded-late arrival == sorted arrival."""
    msgs = make_stream(noise=True, seed=3)

    eng_sorted = FusionEngine()
    ordered = sorted(msgs, key=lambda m: (m["t"], 0 if m["kind"] == "odom" else 1))
    feed(eng_sorted, ordered)

    # exact K-bounded arrival permutation (feasible greedy, see
    # examples/generate_example_inputs.py)
    n = len(ordered)
    k = 12
    import random
    py_rng = random.Random(42)
    used = [False] * n
    perm = []
    for s in range(n):
        forced = s - k
        if forced >= 0 and not used[forced]:
            choice = forced
        else:
            upper = min(n - 1, s + k)
            choice = py_rng.choice(
                [i for i in range(0, upper + 1) if not used[i]])
        used[choice] = True
        perm.append(choice)
    shuffled = [ordered[i] for i in perm]

    eng_shuf = FusionEngine()
    outcomes = feed(eng_shuf, shuffled)
    assert any(o["replayed"] for o in outcomes), "expected checkpointed replays"

    s_a = eng_shuf.state()
    s_b = eng_sorted.state()
    assert np.allclose(np.array(s_a["x"]), np.array(s_b["x"]), atol=1e-12)
    assert np.allclose(np.array(s_a["P"]), np.array(s_b["P"]), atol=1e-12)
    assert s_a["chain_hash"] == s_b["chain_hash"]

    # every per-step trace is identical
    a_steps = eng_shuf.steps_in_order()
    b_steps = eng_sorted.steps_in_order()
    assert [x["message_id"] for x in a_steps] == [x["message_id"] for x in b_steps]
    for x, y in zip(a_steps, b_steps):
        assert np.allclose(x["x"], y["x"], atol=1e-12)
        assert np.allclose(x["innovation"], y["innovation"], atol=1e-12)
        assert x["chain_hash"] == y["chain_hash"]


def test_late_within_window_is_replayed_and_accepted():
    eng = FusionEngine()
    for t, mid in [(0.0, "g0"), (1.0, "g1"), (2.0, "g2"), (3.0, "g3")]:
        r = eng.submit(mid, t, "gnss", V * t, np.array(R_G))
        assert r["status"] == "accepted"
        assert r["replayed"] is False

    # message at t=2.5 arrives after high-water 3.0 -> within 2 s window
    out = eng.submit("late1", 2.5, "gnss", V * 2.5, np.array(R_G))
    assert out["status"] == "accepted"
    assert out["replayed"] is True
    assert out["insert_index"] < len(eng.records) - 1

    # late insertion must change later state consistently; ordered replay matches
    ref = FusionEngine()
    seq = [(0.0, "g0"), (1.0, "g1"), (2.0, "g2"), (2.5, "late1"),
           (3.0, "g3")]
    for t, mid in seq:
        ref.submit(mid, t, "gnss", V * t, np.array(R_G))
    assert eng.state()["x"] == pytest.approx(ref.state()["x"], abs=1e-12)
    assert eng.state()["chain_hash"] == ref.state()["chain_hash"]


def test_message_older_than_window_is_refused():
    eng = FusionEngine()
    eng.submit("g0", 0.0, "gnss", np.zeros(2), np.array(R_G))
    eng.submit("g1", 5.0, "gnss", V * 5.0, np.array(R_G))
    n_records = len(eng.records)
    head = eng.state()["chain_hash"]

    out = eng.submit("old", 5.0 - LATE_WINDOW_S - 0.01, "gnss",
                     np.zeros(2), np.array(R_G))
    assert out["status"] == "rejected"
    assert out["reason"] == "late_too_old"
    assert out["high_water_time"] == pytest.approx(5.0)
    # no state mutation
    assert len(eng.records) == n_records
    assert eng.state()["chain_hash"] == head


def test_long_gap_prediction_and_recovery():
    """A 30 s measurement blackout: prediction must not crash and covariance
    must grow, then fusion must pull the estimate back to truth."""
    eng = FusionEngine(q=1.0)
    rng = np.random.default_rng(5)
    times = np.arange(0.1, 5.0, 0.2)
    for t in times:
        eng.submit(f"g{t}", float(t), "gnss",
                   V * t + rng.normal(scale=0.05, size=2), np.array(R_G))
    p_before = np.array(eng.state()["P"])

    eng.submit("gap-end", 35.0, "gnss", V * 35.0, np.array(R_G))
    p_after = np.array(eng.state()["P"])
    # uncertainty ballooned over the blackout
    assert np.trace(p_after) > np.trace(p_before) * 5
    # symmetric PSD throughout
    assert np.allclose(p_after, p_after.T)
    assert (np.linalg.eigvalsh(p_after) >= -1e-12).all()

    # dense recovery stream
    for t in np.arange(35.2, 45.0, 0.2):
        eng.submit(f"r{t}", float(t), "gnss", V * t, np.array(R_G))
    err = np.linalg.norm(np.array(eng.state()["position"]) - V * 44.8)
    assert err < 1.0


def test_bad_covariance_is_rejected_with_reason():
    # bad matrices are validated at the API layer; emulate that validation
    from ekf_fusion.ekf import validate_covariance
    assert validate_covariance(np.array([[1.0, 0.9], [0.1, 1.0]])) \
        == "covariance_not_symmetric"
    assert validate_covariance(np.array([[1.0, 2.0], [2.0, 1.0]])) \
        == "covariance_not_psd"
    assert validate_covariance(np.full((2, 2), np.nan)) \
        == "covariance_non_finite"
    assert validate_covariance(np.eye(3)) == "covariance_shape"

    # engine itself still works on valid input after those rejections
    eng = FusionEngine()
    out = eng.submit("ok", 0.0, "gnss", np.zeros(2), np.eye(2) * 0.1)
    assert out["status"] == "accepted"


def test_outlier_rejected_and_evidence_retained():
    eng = FusionEngine(gate=DEFAULT_GATE)
    eng.submit("g0", 0.0, "gnss", np.zeros(2), np.array(R_G))
    for t in np.arange(0.2, 3.0, 0.2):
        eng.submit(f"g{t}", float(t), "gnss", V * t, np.array(R_G))
    n_accepted = len(eng.steps)

    blip = eng.submit("blip", 3.0, "gnss", V * 3.0 + np.array([60.0, -50.0]),
                      np.array(R_G))
    assert blip["status"] == "rejected"
    assert blip["reason"] == "outlier_gate"
    assert len(eng.steps) == n_accepted  # state not advanced as a step

    ev = eng.rejections["blip"]
    assert ev["nis"] > DEFAULT_GATE
    assert ev["kind"] == "gnss"
    assert len(ev["innovation"]) == 2
    assert ev["gate"] == pytest.approx(DEFAULT_GATE)

    # subsequent normal message still fuses fine
    nxt = eng.submit("g-next", 3.2, "gnss", V * 3.2, np.array(R_G))
    assert nxt["status"] == "accepted"

    # after an earlier late insertion, the same blip stays rejected and the
    # evidence is reproducible via replay
    eng.submit("late-ok", 2.9, "gnss", V * 2.9, np.array(R_G))
    assert "blip" in eng.rejections
    assert eng.rejections["blip"]["nis"] > DEFAULT_GATE


def test_duplicate_message_id_refused():
    eng = FusionEngine()
    a = eng.submit("same", 0.0, "gnss", np.zeros(2), np.array(R_G))
    b = eng.submit("same", 0.1, "gnss", np.zeros(2), np.array(R_G))
    assert a["status"] == "accepted"
    assert b["status"] == "rejected"
    assert b["reason"] == "duplicate_message_id"


def test_hash_chain_tamper_detection():
    eng = FusionEngine()
    for t in np.arange(0.0, 2.0, 0.2):
        eng.submit(f"g{t}", float(t), "gnss", V * t, np.array(R_G))
    assert eng.verify_chain()["ok"]
    steps = eng.steps_in_order()
    original_x = steps[3]["x"][0]
    steps[3]["x"][0] += 10.0  # tamper with a stored step
    report = eng.verify_chain()
    assert not report["ok"]
    assert steps[3]["message_id"] in report["mismatched_steps"]
    steps[3]["x"][0] = original_x
    assert eng.verify_chain()["ok"]


def test_checkpoints_bounded_and_replay_still_possible():
    eng = FusionEngine(late_window_s=2.0)
    for t in np.arange(0.0, 20.0, 0.1):
        eng.submit(f"g{t:.2f}", float(t), "gnss", V * t, np.array(R_G))
    # baseline retained plus roughly one window of checkpoints, not all 200
    assert len(eng.checkpoints) < 200
    assert eng.baseline_id is not None
    assert eng.baseline_id in eng.checkpoints

    # latest possible in-window late message (high-water 19.9 -> >= 17.9)
    out = eng.submit("late-edge", 17.95, "gnss", V * 17.95, np.array(R_G))
    assert out["status"] == "accepted"
    assert out["replayed"] is True

    # ordered-equivalent consistency after the late insert
    ref = FusionEngine()
    seq = [(float(t), f"g{t:.2f}") for t in np.arange(0.0, 20.0, 0.1)]
    seq.append((17.95, "late-edge"))
    seq.sort(key=lambda x: x[0])
    for t, mid in seq:
        ref.submit(mid, t, "gnss", V * t, np.array(R_G))
    assert eng.state()["x"] == pytest.approx(ref.state()["x"], abs=1e-12)
