"""模板结构校验与提交数据校验（始终按提交时的模板版本执行）。

支持字段类型：text / number / enum / date。
规则：必填 required、条件必填 required_if、数值范围 min/max、枚举 options。
"""
from django.conf import settings
from django.utils.dateparse import parse_date


class SchemaError(ValueError):
    """模板定义非法。"""

    def __init__(self, detail):
        self.detail = detail
        super().__init__(str(detail))


class SubmissionError(ValueError):
    """提交数据未通过模板校验。"""

    def __init__(self, detail):
        # detail 可为字符串或 dict（按字段给出原因）
        self.detail = detail
        super().__init__(str(detail))


FIELD_TYPES = {"text", "number", "enum", "date"}
CONDITION_OPS = {"eq", "ne", "in", "not_in"}


def validate_template_schema(schema):
    """校验模板 schema 本身，返回清洗后的字段定义列表。"""
    if not isinstance(schema, dict):
        raise SchemaError("schema 必须是对象")
    fields = schema.get("fields")
    if not isinstance(fields, list) or not fields:
        raise SchemaError("schema.fields 必须是非空数组")

    max_fields = getattr(settings, "TEMPLATE_MAX_FIELDS", 150)
    if len(fields) > max_fields:
        raise SchemaError(f"每个表单最多 {max_fields} 个字段，当前 {len(fields)} 个")

    seen = set()
    cleaned = []
    for i, field in enumerate(fields):
        loc = f"fields[{i}]"
        if not isinstance(field, dict):
            raise SchemaError(f"{loc} 必须是对象")
        key = field.get("key")
        if not isinstance(key, str) or not key:
            raise SchemaError(f"{loc}.key 必须是非空字符串")
        if key in seen:
            raise SchemaError(f"字段 key 重复: {key}")
        if not key.replace("_", "").isalnum():
            raise SchemaError(f"字段 key 只能包含字母、数字、下划线: {key}")
        seen.add(key)

        ftype = field.get("type")
        if ftype not in FIELD_TYPES:
            raise SchemaError(f"{loc}({key}).type 必须是 {sorted(FIELD_TYPES)}")

        label = field.get("label", key)
        if not isinstance(label, str):
            raise SchemaError(f"{loc}({key}).label 必须是字符串")

        cleaned_field = {
            "key": key,
            "label": label,
            "type": ftype,
            "required": bool(field.get("required", False)),
        }

        if ftype == "number":
            if "min" in field and field["min"] is not None:
                if not isinstance(field["min"], (int, float)) or isinstance(
                    field["min"], bool
                ):
                    raise SchemaError(f"{loc}({key}).min 必须是数值")
                cleaned_field["min"] = field["min"]
            if "max" in field and field["max"] is not None:
                if not isinstance(field["max"], (int, float)) or isinstance(
                    field["max"], bool
                ):
                    raise SchemaError(f"{loc}({key}).max 必须是数值")
                cleaned_field["max"] = field["max"]
            if "min" in cleaned_field and "max" in cleaned_field:
                if cleaned_field["min"] > cleaned_field["max"]:
                    raise SchemaError(f"{loc}({key}) min 不能大于 max")

        if ftype == "enum":
            options = field.get("options")
            if not isinstance(options, list) or not options:
                raise SchemaError(f"{loc}({key}).options 必须是非空数组")
            if len(options) != len(set(map(str, options))):
                raise SchemaError(f"{loc}({key}).options 存在重复值")
            cleaned_field["options"] = options

        rule = field.get("required_if")
        if rule is not None:
            cleaned_rule = _validate_condition(rule, loc, key)
            cleaned_field["required_if"] = cleaned_rule

        cleaned.append(cleaned_field)

    # 条件引用的字段必须存在
    keys = {f["key"] for f in cleaned}
    for f in cleaned:
        rule = f.get("required_if")
        if rule and rule["field"] not in keys:
            raise SchemaError(
                f"字段 {f['key']} 的 required_if 引用了不存在的字段 {rule['field']}"
            )

    return cleaned


def _validate_condition(rule, loc, key):
    if not isinstance(rule, dict):
        raise SchemaError(f"{loc}({key}).required_if 必须是对象")
    field = rule.get("field")
    op = rule.get("op", "eq")
    if not isinstance(field, str) or not field:
        raise SchemaError(f"{loc}({key}).required_if.field 必须是非空字符串")
    if op not in CONDITION_OPS:
        raise SchemaError(
            f"{loc}({key}).required_if.op 必须是 {sorted(CONDITION_OPS)}"
        )
    if "value" not in rule:
        raise SchemaError(f"{loc}({key}).required_if 缺少 value")
    value = rule["value"]
    if op in {"in", "not_in"}:
        if not isinstance(value, list) or not value:
            raise SchemaError(f"{loc}({key}).required_if.value 必须是非空数组")
    return {"field": field, "op": op, "value": value}


def _condition_matches(data, rule):
    actual = data.get(rule["field"])
    op, expected = rule["op"], rule["value"]
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op == "in":
        return actual in expected
    if op == "not_in":
        return actual not in expected
    return False


def validate_submission(template, data):
    """按指定模板版本校验客户端提交内容。

    历史版本始终按历史模板校验；被新版删除但加入 legacy_fields 的字段，
    旧设备仍可提交（跳过类型检查，原样保留）。
    """
    if not isinstance(data, dict):
        raise SubmissionError("data 必须是对象")

    fields = template.field_defs
    known_keys = {f["key"] for f in fields}
    legacy_keys = set(template.legacy_fields or [])

    unknown = set(data.keys()) - known_keys - legacy_keys
    if unknown:
        raise SubmissionError(
            {"_": f"存在模板 {template.code} v{template.version} 不认识的字段",
             "unknown_fields": sorted(unknown)}
        )

    errors = {}
    for field in fields:
        key = field["key"]
        present = key in data and data[key] is not None and data[key] != ""

        if not present:
            required = field.get("required", False)
            if not required and field.get("required_if"):
                required = _condition_matches(data, field["required_if"])
            if required:
                errors[key] = "必填（含条件必填）"
            continue

        value = data[key]
        ftype = field["type"]

        if ftype == "text":
            if not isinstance(value, str):
                errors[key] = "必须是文本"
        elif ftype == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors[key] = "必须是数值"
            else:
                if "min" in field and value < field["min"]:
                    errors[key] = f"不能小于 {field['min']}"
                if "max" in field and value > field["max"]:
                    errors[key] = f"不能大于 {field['max']}"
        elif ftype == "enum":
            if value not in field["options"]:
                errors[key] = f"必须是枚举值之一: {field['options']}"
        elif ftype == "date":
            parsed = parse_date(value) if isinstance(value, str) else None
            if not isinstance(value, str) or parsed is None or len(value) != 10:
                errors[key] = "必须是 YYYY-MM-DD 日期字符串"

    if errors:
        raise SubmissionError(errors)

    return True


def analyze_compatibility(old_fields, new_cleaned_fields):
    """发布新版本时对照上一版本生成兼容策略与硬性拦截。

    返回 (legacy_fields, warnings, blocking_errors)。

    * 字段被删除          -> 加入 legacy_fields 白名单（旧设备提交不丢、不报错），
                            并给出告警。
    * 字段类型被改变      -> 阻断发布（旧数据语义无法保证）。
    * 枚举选项被移除      -> 阻断发布（旧设备可能仍提交该值）。
    * 新增必填/收紧规则   -> 允许发布但告警：旧版本的提交仍按旧版本校验，
                            规则只对新版本提交生效，旧提交不会因此失败或丢失。
    """
    old_by_key = {f["key"]: f for f in (old_fields or [])}
    new_by_key = {f["key"]: f for f in new_cleaned_fields}

    legacy_fields = []
    warnings = []
    blocking = []

    for key, old in old_by_key.items():
        new = new_by_key.get(key)
        if new is None:
            legacy_fields.append(key)
            warnings.append(
                f"字段 {key} 在新版本中被删除：已加入遗留白名单，"
                "旧设备仍可提交该字段，数据原样保留但不再参与新版本校验"
            )
            continue
        if new["type"] != old["type"]:
            blocking.append(
                f"字段 {key} 类型从 {old['type']} 改为 {new['type']}："
                "不允许直接修改类型，请删除旧字段并新增字段"
            )
        if new["type"] == "enum":
            removed_opts = [o for o in old.get("options", []) if o not in new["options"]]
            if removed_opts:
                blocking.append(
                    f"枚举字段 {key} 删除了选项 {removed_opts}："
                    "旧设备可能仍提交这些值，请保留选项或改用新字段"
                )
        if new["type"] == "number":
            old_min, old_max = old.get("min"), old.get("max")
            if old_min is not None and (
                new.get("min") is None or new["min"] > old_min
            ):
                blocking.append(
                    f"数值字段 {key} 收紧了下限（{old_min} -> {new.get('min')}），"
                    "旧设备合法提交可能被拒"
                )
            if old_max is not None and (
                new.get("max") is None or new["max"] < old_max
            ):
                blocking.append(
                    f"数值字段 {key} 收紧了上限（{old_max} -> {new.get('max')}），"
                    "旧设备合法提交可能被拒"
                )
        # 必填收紧只告警：旧版本提交按旧模板走，不会被破坏。
        if not old.get("required") and new.get("required"):
            warnings.append(
                f"字段 {key} 由选填变为必填：仅对新版本提交生效，"
                "旧版本提交仍按其当时模板校验"
            )
        old_rule = old.get("required_if")
        new_rule = new.get("required_if")
        if not old_rule and new_rule:
            warnings.append(
                f"字段 {key} 新增条件必填规则：仅对新版本提交生效"
            )

    for key, new in new_by_key.items():
        if key not in old_by_key and new.get("required"):
            warnings.append(
                f"新增必填字段 {key}：使用旧版本的设备不会提交该字段，"
                "旧版本提交按旧模板校验，不会因此丢失"
            )

    return sorted(legacy_fields), warnings, blocking
