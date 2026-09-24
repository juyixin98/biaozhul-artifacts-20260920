"""Label-selector evaluation: real set-based Kubernetes selector semantics.

Nothing here works by string matching on names/namespaces — selectors are
evaluated as the conjunction of equality requirements (``matchLabels``) and
set-based requirements (``matchExpressions``) over label maps.
"""

from __future__ import annotations

from typing import Mapping

from .models import LabelSelector


def selector_matches(selector: LabelSelector, labels: Mapping[str, str]) -> bool:
    """Return True iff *labels* satisfy every requirement of *selector*.

    - ``matchLabels`` is short-hand for one ``In`` (single value) requirement
      per key; every key must be present with the exact value.
    - ``In``       : key present and its value is in the value set
    - ``NotIn``    : key absent, or present with a value outside the set
    - ``Exists``   : key present (any value, including empty)
    - ``DoesNotExist`` : key absent
    An empty selector (no requirements) matches everything.
    """
    for key, expected in selector.match_labels.items():
        if labels.get(key) != expected:
            return False

    for req in selector.match_expressions:
        present = req.key in labels
        value = labels.get(req.key)
        if req.operator == "In":
            if not present or value not in req.values:
                return False
        elif req.operator == "NotIn":
            if present and value in req.values:
                return False
        elif req.operator == "Exists":
            if not present:
                return False
        elif req.operator == "DoesNotExist":
            if present:
                return False
        else:  # validated at the model layer; defensive only
            raise ValueError(f"unsupported selector operator: {req.operator}")
    return True
