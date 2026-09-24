"""分块窗口测试。"""

import pytest

from delay_correlator.window import iter_windows


def test_single_window_when_none():
    wins = iter_windows(1000, None)
    assert len(wins) == 1
    assert (wins[0].start, wins[0].end) == (0, 1000)


def test_signal_shorter_than_window_is_one_window():
    wins = iter_windows(500, window_size=1000)
    assert len(wins) == 1
    assert wins[0].length == 500


def test_exact_division():
    wins = iter_windows(1000, window_size=250)
    assert [(w.start, w.end) for w in wins] == [
        (0, 250), (250, 500), (500, 750), (750, 1000)
    ]
    assert [w.index for w in wins] == [0, 1, 2, 3]


def test_overlapping_hop():
    wins = iter_windows(100, window_size=40, hop=20)
    assert [(w.start, w.end) for w in wins] == [
        (0, 40), (20, 60), (40, 80), (60, 100)
    ]


def test_partial_tail_kept_above_half():
    # 末尾余 250 >= 400*0.5=200 -> 保留
    wins = iter_windows(1050, window_size=400, hop=400)
    assert [(w.start, w.end) for w in wins] == [(0, 400), (400, 800), (800, 1050)]


def test_partial_tail_dropped_below_half():
    # 末尾余 100 < 400*0.5=200 -> 丢弃
    wins = iter_windows(900, window_size=400, hop=400)
    assert [(w.start, w.end) for w in wins] == [(0, 400), (400, 800)]


def test_empty_signal():
    assert iter_windows(0, window_size=100) == []


@pytest.mark.parametrize("bad_ws,bad_hop", [(0, None), (-1, None), (100, 0)])
def test_invalid_params(bad_ws, bad_hop):
    with pytest.raises(ValueError):
        iter_windows(1000, window_size=bad_ws, hop=bad_hop)
