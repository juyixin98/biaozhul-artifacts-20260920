from app.filters import taint_tolerated
from app.models import Taint, Toleration


def taint(key="k", value="v", effect="NoSchedule"):
    return Taint(key=key, value=value, effect=effect)


def test_equal_toleration():
    assert taint_tolerated(taint(), Toleration(key="k", operator="Equal", value="v"))
    assert not taint_tolerated(taint(), Toleration(key="k", operator="Equal", value="x"))
    assert not taint_tolerated(taint(), Toleration(key="other", operator="Equal", value="v"))


def test_exists_toleration_any_value():
    assert taint_tolerated(taint(), Toleration(key="k", operator="Exists"))
    assert not taint_tolerated(taint(), Toleration(key="other", operator="Exists"))


def test_wildcard_exists_tolerates_everything():
    assert taint_tolerated(taint(), Toleration(operator="Exists"))
    assert taint_tolerated(taint(key="anything", value="z", effect="NoExecute"), Toleration(operator="Exists"))


def test_effect_scoping():
    assert not taint_tolerated(
        taint(effect="NoExecute"),
        Toleration(key="k", operator="Equal", value="v", effect="NoSchedule"),
    )
    assert taint_tolerated(
        taint(effect="NoSchedule"),
        Toleration(key="k", operator="Equal", value="v", effect=""),
    )
