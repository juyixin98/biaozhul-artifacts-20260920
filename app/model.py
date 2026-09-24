"""Policy document model: parsing and static validation of rule sets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import PolicyValidationError
from .interpreter import validate_node

#: Canonical effect vocabulary. Input also accepts the alias "allow".
PERMIT = "permit"
DENY = "deny"
_EFFECT_ALIASES = {"permit": PERMIT, "allow": PERMIT, "deny": DENY}


@dataclass(frozen=True)
class Rule:
    id: str
    effect: str  # "permit" | "deny"
    when: dict[str, Any]
    description: str = ""


@dataclass(frozen=True)
class Policy:
    id: str
    rules: tuple[Rule, ...]
    description: str = ""
    combining_algorithm: str = "deny_overrides"

    def with_rules(self, rules: tuple[Rule, ...]) -> "Policy":
        return Policy(
            id=self.id,
            rules=tuple(rules),
            description=self.description,
            combining_algorithm=self.combining_algorithm,
        )


def parse_rule(raw: Any, *, index: int) -> Rule:
    path = f"rules[{index}]"
    if not isinstance(raw, dict):
        raise PolicyValidationError("rule must be an object", path=path)
    rid = raw.get("id")
    if not isinstance(rid, str) or not rid:
        raise PolicyValidationError("rule 'id' must be a non-empty string",
                                    path=f"{path}.id")
    effect_raw = raw.get("effect")
    if not isinstance(effect_raw, str):
        raise PolicyValidationError(
            "rule 'effect' must be 'permit' (alias 'allow') or 'deny'",
            path=f"{path}.effect",
        )
    effect = _EFFECT_ALIASES.get(effect_raw)
    if effect is None:
        raise PolicyValidationError(
            f"unknown rule effect {effect_raw!r}; expected permit/deny",
            path=f"{path}.effect",
        )
    when = raw.get("when")
    validate_node(when, path=f"{path}.when")
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise PolicyValidationError("rule 'description' must be a string",
                                    path=f"{path}.description")
    extra = set(raw) - {"id", "effect", "when", "description"}
    if extra:
        raise PolicyValidationError(f"unexpected field(s) {sorted(extra)}",
                                    path=path)
    return Rule(id=rid, effect=effect, when=when, description=description)


def parse_policy(raw: Any) -> Policy:
    if not isinstance(raw, dict):
        raise PolicyValidationError("policy must be an object")
    pid = raw.get("id")
    if not isinstance(pid, str) or not pid:
        raise PolicyValidationError("policy 'id' must be a non-empty string",
                                    path="id")
    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, list):
        raise PolicyValidationError("policy 'rules' must be a list",
                                    path="rules")
    rules = tuple(parse_rule(r, index=i) for i, r in enumerate(rules_raw))
    seen: set[str] = set()
    for r in rules:
        if r.id in seen:
            raise PolicyValidationError(
                f"duplicate rule id {r.id!r} in policy {pid!r}",
                path="rules",
            )
        seen.add(r.id)

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise PolicyValidationError("policy 'description' must be a string",
                                    path="description")
    combining = raw.get("combining_algorithm", "deny_overrides")
    if combining != "deny_overrides":
        raise PolicyValidationError(
            f"unsupported combining_algorithm {combining!r}; "
            "only 'deny_overrides' is implemented",
            path="combining_algorithm",
        )
    extra = set(raw) - {"id", "rules", "description", "combining_algorithm"}
    if extra:
        raise PolicyValidationError(f"unexpected field(s) {sorted(extra)}")
    return Policy(
        id=pid,
        rules=rules,
        description=description,
        combining_algorithm=combining,
    )


def parse_policy_set(raw: Any) -> tuple[Policy, ...]:
    """Parse ``{"policies": [policy, ...]}`` with unique policy ids."""
    if not isinstance(raw, dict):
        raise PolicyValidationError("policy set must be an object")
    items = raw.get("policies")
    if not isinstance(items, list) or not items:
        raise PolicyValidationError(
            "'policies' must be a non-empty list", path="policies"
        )
    extra = set(raw) - {"policies"}
    if extra:
        raise PolicyValidationError(f"unexpected field(s) {sorted(extra)}")
    policies = tuple(parse_policy(p) for p in items)
    ids = [p.id for p in policies]
    if len(set(ids)) != len(ids):
        raise PolicyValidationError("duplicate policy id in policy set",
                                    path="policies")
    return policies
