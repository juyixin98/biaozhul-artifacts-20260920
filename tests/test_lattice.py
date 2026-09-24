from app.analysis.taint import Taint


def test_bottom_is_join_neutral():
    assert Taint.join(Taint.BOTTOM, Taint.CLEAN) is Taint.CLEAN
    assert Taint.join(Taint.TAINT, Taint.BOTTOM) is Taint.TAINT
    assert Taint.join(Taint.MAYBE, Taint.BOTTOM) is Taint.MAYBE


def test_join_identity():
    assert Taint.join(Taint.CLEAN, Taint.CLEAN) is Taint.CLEAN
    assert Taint.join(Taint.TAINT, Taint.TAINT) is Taint.TAINT
    assert Taint.join(Taint.MAYBE, Taint.MAYBE) is Taint.MAYBE
    assert Taint.join(Taint.BOTTOM, Taint.BOTTOM) is Taint.BOTTOM


def test_join_clean_and_taint_is_maybe():
    # Two *defined* sibling paths (one clean, one tainted) -> MAYBE.
    assert Taint.join(Taint.CLEAN, Taint.TAINT) is Taint.MAYBE
    assert Taint.join(Taint.TAINT, Taint.CLEAN) is Taint.MAYBE


def test_join_maybe_absorbs():
    assert Taint.join(Taint.MAYBE, Taint.CLEAN) is Taint.MAYBE
    assert Taint.join(Taint.MAYBE, Taint.TAINT) is Taint.MAYBE


def test_join_associative():
    a = Taint.join(Taint.join(Taint.CLEAN, Taint.TAINT), Taint.CLEAN)
    b = Taint.join(Taint.CLEAN, Taint.join(Taint.TAINT, Taint.CLEAN))
    assert a is b is Taint.MAYBE
    # bottom neutrality must also associate
    c = Taint.join(Taint.join(Taint.BOTTOM, Taint.TAINT), Taint.BOTTOM)
    assert c is Taint.TAINT
