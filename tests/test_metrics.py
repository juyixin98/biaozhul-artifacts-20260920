"""Metric accounting: unknown handling and two-sided scoring."""

from app.segmentation.metrics import evaluate, evaluate_report

G, N, U = "ground", "non_ground", "unknown"


def test_perfect_classification():
    truth = [G, G, N, N]
    m = evaluate(truth, truth)
    assert (m.precision, m.recall, m.f1) == (1.0, 1.0, 1.0)
    assert (m.tp, m.fp, m.fn, m.tn) == (2, 0, 0, 2)


def test_unknown_prediction_is_abstention():
    # Unknown on a ground point: FN for ground; on non-ground: FP for ground.
    m = evaluate([U, U], [G, N])
    assert m.tp == 0 and m.fp == 1 and m.fn == 1
    assert (m.precision, m.recall) == (0.0, 0.0)
    m_ng = evaluate([U, U], [G, N], positive=N)
    assert m_ng.fp == 1 and m_ng.fn == 1


def test_unknown_truth_excluded():
    m = evaluate([G, N], [U, U])
    assert (m.tp, m.fp, m.fn, m.tn) == (0, 0, 0, 0)


def test_full_report_counts():
    # pred:  G    N    U    G
    # truth: G    N    N    N
    rep = evaluate_report([G, N, U, G], [G, N, N, N])
    assert rep.total_points == 4
    assert rep.unknown_predictions == 1
    # Ground-as-positive: fp=2 (idx3 G predicted on true N, abstention idx2
    # on true N); tn=1 (idx1); fn=0 (the only true G, idx0, was found).
    assert rep.ground.tp == 1
    assert rep.ground.fp == 2
    assert rep.ground.fn == 0
    assert rep.ground.tn == 1
    # Non-ground-as-positive: tp=1 (idx1); fn=2 (idx3 and abstention idx2).
    assert rep.non_ground.tp == 1
    assert rep.non_ground.fn == 2
    assert rep.non_ground.fp == 0
    assert rep.label_distribution == {G: 2, N: 1, U: 1}
