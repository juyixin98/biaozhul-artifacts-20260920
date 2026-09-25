"""策略数据模型：Rule（字段规则）与 Policy（策略版本）。

路径表示
--------
字段路径用点分字符串表示，数组元素统一写作 ``[]``::

    user            -> ["user"]
    user.addr.city  -> ["user", "addr", "city"]
    contacts[].email -> ["contacts", "[]", "email"]

即真实数据中的任意数组下标在规则中都写成 ``[]``；``[]`` 不允许作为
普通字段名。规则路径与数据路径都在匹配前“规范化”，因此别名无法通过
改用下标/点号写法绕过限制。

别名
----
Policy.aliases 是 {规范名: [别名...]}。匹配时同一个数据路径会分别用
真实段名、规范段名解析，任一命中即按规则处理；别名命中也会在决策记录
中以 matched_path 如实标注。
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from .canonical import canonical_dumps
from .errors import PolicyValidationError

# 合法动作。
ACTIONS = ("allow", "deny", "generalize")

# 合法泛化器名（generalizers 模块的注册表会再次校验）。
GENERALIZER_NAMES = {
    "redact",
    "mask",
    "email_mask",
    "date_bucket",
    "numeric_bucket",
    "hash",
    "category",
    "replace",
}

# 路径段中保留的段名。
ARRAY_SEGMENT = "[]"


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_path(path: str) -> list[str]:
    """解析点分路径为段列表。

    user.addr -> ["user", "addr"]；contacts[].email -> ["contacts", "[]", "email"]。
    """
    if not isinstance(path, str) or path == "":
        raise PolicyValidationError(f"invalid path {path!r}: must be non-empty string")
    # 先按 [..] 拆分，再对点分段。
    segs: list[str] = []
    cur = ""
    i = 0
    while i < len(path):
        ch = path[i]
        if ch == ".":
            if cur == "":
                raise PolicyValidationError(f"invalid path {path!r}: empty segment")
            segs.append(cur)
            cur = ""
            i += 1
        elif ch == "[":
            # '[]' 必须紧跟在字段名、另一个 '[]' 之后，或位于路径开头
            #（根是数组的情形，如 [].id）。
            if not cur and segs and segs[-1] != ARRAY_SEGMENT:
                raise PolicyValidationError(
                    f"invalid path {path!r}: '[]' must follow a field name "
                    "or another '[]'")
            if cur:
                segs.append(cur)
                cur = ""
            j = path.find("]", i + 1)
            if j == -1:
                raise PolicyValidationError(f"invalid path {path!r}: missing ']'")
            inner = path[i + 1:j]
            if inner != "":
                raise PolicyValidationError(
                    f"invalid path {path!r}: array index must be '[]', got [{inner}]"
                )
            segs.append(ARRAY_SEGMENT)
            i = j + 1
            # [] 后若紧跟 '.'，消费它，下一段从点号之后开始；
            # 紧跟另一个 '['（多维数组 [][]）也合法。
            if i < len(path) and path[i] == ".":
                i += 1
        elif ch == "]":
            raise PolicyValidationError(
                f"invalid path {path!r}: unexpected ']' without preceding '['")
        else:
            cur += ch
            i += 1
    if cur:
        segs.append(cur)
    if not segs:
        raise PolicyValidationError(f"invalid path {path!r}")
    for s in segs:
        if "." in s:  # pragma: no cover - parse 已保证
            raise PolicyValidationError(f"invalid segment {s!r}")
    return segs


def render_path(segs: Iterable[str]) -> str:
    """段列表反向渲染为点分字符串（数组段渲染为 []）。"""
    out = ""
    for s in segs:
        if s == ARRAY_SEGMENT:
            out += "[]"  # [] 直接接在前段之后，如 contacts[].phone
        else:
            if out:
                out += "."
            out += s
    return out


@dataclass(frozen=True)
class Rule:
    """单条字段规则。

    purpose 为 None 表示对所有用途生效；否则仅在该用途下生效。
    recursive=True 时规则对“该路径及其全部子孙路径”生效。
    """

    path: str
    action: str
    generalizer: str | None = None
    params: tuple[tuple[str, Any], ...] = ()
    purpose: str | None = None
    recursive: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        # 冻结对象上赋值需用 object.__setattr__；这里只做校验。
        if self.action not in ACTIONS:
            raise PolicyValidationError(
                f"rule on {self.path!r}: unknown action {self.action!r}"
            )
        if self.action == "generalize":
            if not self.generalizer:
                raise PolicyValidationError(
                    f"rule on {self.path!r}: generalize requires a generalizer"
                )
            if self.generalizer not in GENERALIZER_NAMES:
                raise PolicyValidationError(
                    f"rule on {self.path!r}: unknown generalizer {self.generalizer!r}"
                )
        elif self.generalizer is not None:
            raise PolicyValidationError(
                f"rule on {self.path!r}: generalizer only valid with action 'generalize'"
            )
        # 触发路径解析以在加载期拒绝非法路径。
        parse_path(self.path)

    @property
    def params_dict(self) -> dict[str, Any]:
        return dict(self.params)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "path": self.path,
            "action": self.action,
        }
        if self.generalizer is not None:
            d["generalizer"] = self.generalizer
        if self.params:
            d["params"] = {k: v for k, v in self.params}
        if self.purpose is not None:
            d["purpose"] = self.purpose
        if self.recursive:
            d["recursive"] = True
        if self.description:
            d["description"] = self.description
        return d

    @staticmethod
    def from_dict(d: Any) -> "Rule":
        if not isinstance(d, dict):
            raise PolicyValidationError("rule must be an object")
        required = ("path", "action")
        for k in required:
            if k not in d:
                raise PolicyValidationError(f"rule missing required field {k!r}")
        params = d.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise PolicyValidationError(f"rule {d.get('path')!r}: params must be object")
        # params 转为有序元组，保证指纹稳定。
        params_tuple = tuple(sorted(
            ((k, _ensure_jsonable(k, v)) for k, v in params.items()),
            key=lambda kv: kv[0],
        ))
        return Rule(
            path=d["path"],
            action=d["action"],
            generalizer=d.get("generalizer"),
            params=params_tuple,
            purpose=d.get("purpose"),
            recursive=bool(d.get("recursive", False)),
            description=str(d.get("description", "")),
        )


def _ensure_jsonable(ctx: str, v: Any) -> Any:
    if v is None or isinstance(v, (str, bool, int, float)):
        return v
    if isinstance(v, list):
        return [_ensure_jsonable(ctx, x) for x in v]
    if isinstance(v, dict):
        return {str(k): _ensure_jsonable(str(k), x) for k, x in sorted(v.items())}
    raise PolicyValidationError(f"params value for {ctx!r} not JSON-serializable: {v!r}")


@dataclass(frozen=True)
class Policy:
    """不可变策略版本。

    策略的每次发布产生新版本；已发布版本内容不可修改（immutable），
    导出任务固定到某个具体版本，因此导出期不受后续更新影响。
    """

    policy_id: str
    version: int
    rules: tuple[Rule, ...]
    aliases: tuple[tuple[str, tuple[str, ...]], ...] = ()
    default_action: str = "deny"
    created_at: str = field(default_factory=now_iso)
    description: str = ""
    fingerprint: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.policy_id, str) or not self.policy_id:
            raise PolicyValidationError("policy_id must be non-empty string")
        if not isinstance(self.version, int) or self.version < 1:
            raise PolicyValidationError("version must be int >= 1")
        if self.default_action not in ("deny", "allow"):
            raise PolicyValidationError(
                f"default_action must be 'deny' or 'allow', got {self.default_action!r}"
            )
        self._validate_aliases()
        # fingerprint 是缓存字段；空则按规范化内容计算。
        fp = policy_fingerprint(
            self.policy_id,
            self.version,
            tuple(self.rules),
            tuple(self.aliases),
            self.default_action,
            self.created_at,
            self.description,
        )
        object.__setattr__(self, "fingerprint", fp)

    def _validate_aliases(self) -> None:
        seen_canonical: set[str] = set()
        all_names: dict[str, str] = {}  # 任一名称 -> 规范名，检测冲突
        for canonical, alias_list in self.aliases:
            if not canonical or not isinstance(canonical, str):
                raise PolicyValidationError("alias canonical name must be non-empty string")
            if canonical in seen_canonical:
                raise PolicyValidationError(f"duplicate alias target {canonical!r}")
            seen_canonical.add(canonical)
            names = [canonical, *alias_list]
            for n in names:
                if n == ARRAY_SEGMENT:
                    raise PolicyValidationError("'[]' cannot be a field name/alias")
                if n in all_names and all_names[n] != canonical:
                    raise PolicyValidationError(
                        f"alias {n!r} maps to both {all_names[n]!r} and {canonical!r}"
                    )
                all_names[n] = canonical

    @property
    def alias_map(self) -> dict[str, str]:
        """{别名或规范名: 规范名}。"""
        m: dict[str, str] = {}
        for canonical, aliases in self.aliases:
            m[canonical] = canonical
            for a in aliases:
                m[a] = canonical
        return m

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "default_action": self.default_action,
            "description": self.description,
            "created_at": self.created_at,
            "fingerprint": self.fingerprint,
            "aliases": [
                {"canonical": c, "aliases": list(a)} for c, a in self.aliases
            ],
            "rules": [r.to_dict() for r in self.rules],
        }

    @staticmethod
    def from_dict(d: Any) -> "Policy":
        if not isinstance(d, dict):
            raise PolicyValidationError("policy must be an object")
        aliases_raw = d.get("aliases", [])
        aliases: list[tuple[str, tuple[str, ...]]] = []
        for item in aliases_raw:
            if isinstance(item, dict):
                c = item.get("canonical")
                al = item.get("aliases", [])
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                c, al = item
            else:
                raise PolicyValidationError(f"bad alias entry: {item!r}")
            if not isinstance(al, list) or not all(isinstance(x, str) for x in al):
                raise PolicyValidationError(f"alias list for {c!r} must be strings")
            aliases.append((str(c), tuple(al)))
        rules_raw = d.get("rules", [])
        if not isinstance(rules_raw, list):
            raise PolicyValidationError("rules must be a list")
        return Policy(
            policy_id=d["policy_id"],
            version=int(d["version"]),
            rules=tuple(Rule.from_dict(r) for r in rules_raw),
            aliases=tuple(aliases),
            default_action=d.get("default_action", "deny"),
            created_at=d.get("created_at", now_iso()),
            description=str(d.get("description", "")),
        )


def policy_fingerprint(
    policy_id: str,
    version: int,
    rules: tuple[Rule, ...],
    aliases: tuple[tuple[str, tuple[str, ...]], ...],
    default_action: str,
    created_at: str,
    description: str,
) -> str:
    """对策略的全部决策相关内容计算 SHA-256（hex）。

    fingerprint 覆盖规则、别名、默认动作与版本标识；created_at/description
    也纳入以防同内容不同描述被误当作同一版本。
    """
    doc = {
        "policy_id": policy_id,
        "version": version,
        "default_action": default_action,
        "description": description,
        "created_at": created_at,
        "aliases": [[c, list(a)] for c, a in aliases],
        "rules": [r.to_dict() for r in rules],
    }
    return hashlib.sha256(canonical_dumps(doc)).hexdigest()
