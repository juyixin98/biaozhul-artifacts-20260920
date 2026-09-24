from app.models import LabelSelector, LabelSelectorRequirement
from app.selectors import selector_matches

LABELS = {"tier": "web", "zone": "a"}


def test_match_labels():
    assert selector_matches(LabelSelector(match_labels={"tier": "web"}), LABELS)
    assert not selector_matches(LabelSelector(match_labels={"tier": "db"}), LABELS)


def test_operators():
    assert selector_matches(LabelSelector(match_expressions=[
        LabelSelectorRequirement(key="zone", operator="In", values=["a", "b"])
    ]), LABELS)
    assert selector_matches(LabelSelector(match_expressions=[
        LabelSelectorRequirement(key="zone", operator="NotIn", values=["x"])
    ]), LABELS)
    # Missing key satisfies NotIn (Kubernetes semantics).
    assert selector_matches(LabelSelector(match_expressions=[
        LabelSelectorRequirement(key="missing", operator="NotIn", values=["x"])
    ]), LABELS)
    assert selector_matches(LabelSelector(match_expressions=[
        LabelSelectorRequirement(key="tier", operator="Exists")
    ]), LABELS)
    assert selector_matches(LabelSelector(match_expressions=[
        LabelSelectorRequirement(key="other", operator="DoesNotExist")
    ]), LABELS)
    assert not selector_matches(LabelSelector(match_expressions=[
        LabelSelectorRequirement(key="tier", operator="DoesNotExist")
    ]), LABELS)
