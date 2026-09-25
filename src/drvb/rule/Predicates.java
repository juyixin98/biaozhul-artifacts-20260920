package drvb.rule;

import drvb.json.Json;
import drvb.json.JsonException;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 谓词编译 / 求值：把规则 JSON 编译成不可变 {@link Predicate} 树。
 *
 * <p>支持的规约（叶子节点 {@code {"op": "...", "field": "a.b", ...}}）：
 * <ul>
 *   <li>{@code eq} / {@code ne}：相等 / 不等；数字按数值比较（1 == 1.0）</li>
 *   <li>{@code gt} / {@code gte} / {@code lt} / {@code lte}：数值比较</li>
 *   <li>{@code in} / {@code notIn}：值是否在 {@code values} 数组中</li>
 *   <li>{@code contains}：字符串子串；{@code startsWith} / {@code endsWith}：前后缀</li>
 *   <li>{@code isnull}：字段缺失或值为 JSON null</li>
 * </ul>
 * 组合节点：{@code {"op":"and"|"or","predicates":[...]}}、
 * {@code {"op":"not","predicate":{...}}}；常量：{@code {"op":"true"|"false"}}。
 *
 * <p>语义约定：比较类算子遇到字段缺失、null 或类型不符时结果为 false（不抛异常）；
 * 规约本身非法（未知 op、缺参数等）在发布时抛 {@link RuleException}。
 */
public final class Predicates {

    private Predicates() {
    }

    /** 把规则 JSON（已解析的 Map）编译为谓词树。 */
    @SuppressWarnings("unchecked")
    public static Predicate compile(Map<String, Object> spec) {
        if (spec == null) {
            throw new RuleException("规则谓词不能为空");
        }
        Object opObj = spec.get("op");
        if (!(opObj instanceof String)) {
            throw new RuleException("谓词缺少字符串字段 'op'");
        }
        String op = (String) opObj;
        switch (op) {
            case "true":
                return Predicate.always(true);
            case "false":
                return Predicate.always(false);
            case "and":
                return all(compileList(spec.get("predicates"), op));
            case "or":
                return any(compileList(spec.get("predicates"), op));
            case "not": {
                Object inner = spec.get("predicate");
                if (!(inner instanceof Map)) {
                    throw new RuleException("'not' 谓词需要对象字段 'predicate'");
                }
                return compile((Map<String, Object>) inner).negate();
            }
            case "eq":
                return compareLeaf(spec, Compare.EQ);
            case "ne":
                return compareLeaf(spec, Compare.NE);
            case "gt":
                return compareLeaf(spec, Compare.GT);
            case "gte":
                return compareLeaf(spec, Compare.GTE);
            case "lt":
                return compareLeaf(spec, Compare.LT);
            case "lte":
                return compareLeaf(spec, Compare.LTE);
            case "in":
            case "notIn": {
                Path path = pathOf(spec);
                Object rawValues = spec.get("values");
                if (!(rawValues instanceof List)) {
                    throw new RuleException("'" + op + "' 谓词需要数组字段 'values'");
                }
                List<Object> values = Json.asArray(rawValues);
                boolean negate = op.equals("notIn");
                return ctx -> {
                    Object actual = path.resolve(ctx);
                    if (actual == null || actual == Json.NULL) {
                        return false;
                    }
                    boolean found = false;
                    for (Object v : values) {
                        if (looseEquals(actual, v)) {
                            found = true;
                            break;
                        }
                    }
                    return found != negate;
                };
            }
            case "contains":
            case "startsWith":
            case "endsWith": {
                Path path = pathOf(spec);
                String needle = Json.getString(spec, "value");
                switch (op) {
                    case "contains":
                        return ctx -> {
                            String s = asStringOrNull(path.resolve(ctx));
                            return s != null && s.contains(needle);
                        };
                    case "startsWith":
                        return ctx -> {
                            String s = asStringOrNull(path.resolve(ctx));
                            return s != null && s.startsWith(needle);
                        };
                    default:
                        return ctx -> {
                            String s = asStringOrNull(path.resolve(ctx));
                            return s != null && s.endsWith(needle);
                        };
                }
            }
            case "isnull": {
                Path path = pathOf(spec);
                return ctx -> {
                    Object v = path.resolve(ctx);
                    return v == null || v == Json.NULL;
                };
            }
            default:
                throw new RuleException("未知谓词算子: " + op);
        }
    }

    private static Predicate all(List<Predicate> ps) {
        return ctx -> {
            for (Predicate p : ps) {
                if (!p.test(ctx)) {
                    return false;
                }
            }
            return true;
        };
    }

    private static Predicate any(List<Predicate> ps) {
        return ctx -> {
            for (Predicate p : ps) {
                if (p.test(ctx)) {
                    return true;
                }
            }
            return false;
        };
    }

    private static List<Predicate> compileList(Object raw, String op) {
        if (!(raw instanceof List)) {
            throw new RuleException("'" + op + "' 谓词需要数组字段 'predicates'");
        }
        List<Object> rawList = Json.asArray(raw);
        if (rawList.isEmpty()) {
            throw new RuleException("'" + op + "' 谓词的 'predicates' 数组不能为空");
        }
        List<Predicate> out = new ArrayList<>(rawList.size());
        for (Object item : rawList) {
            if (!(item instanceof Map)) {
                throw new RuleException("'" + op + "' 的子谓词必须是对象");
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) item;
            out.add(compile(m));
        }
        return out;
    }

    private static Predicate compareLeaf(Map<String, Object> spec, Compare cmp) {
        Path path = pathOf(spec);
        if (!spec.containsKey("value")) {
            throw new RuleException("'" + cmp.token + "' 谓词需要 'value' 字段");
        }
        Object expected = spec.get("value");
        if (expected == Json.NULL) {
            throw new RuleException("'" + cmp.token + "' 谓词的 value 不能为 null（请用 isnull）");
        }
        if (cmp == Compare.EQ || cmp == Compare.NE) {
            return ctx -> {
                Object actual = path.resolve(ctx);
                boolean eq = looseEquals(actual, expected);
                return cmp == Compare.EQ ? eq : !eq;
            };
        }
        if (!(expected instanceof Number)) {
            throw new RuleException("'" + cmp.token + "' 谓词的 value 必须是数字");
        }
        double exp = ((Number) expected).doubleValue();
        return ctx -> {
            Object actual = path.resolve(ctx);
            if (!(actual instanceof Number)) {
                return false;
            }
            double act = ((Number) actual).doubleValue();
            switch (cmp) {
                case GT:
                    return act > exp;
                case GTE:
                    return act >= exp;
                case LT:
                    return act < exp;
                case LTE:
                    return act <= exp;
                default:
                    return false;
            }
        };
    }

    private static Path pathOf(Map<String, Object> spec) {
        String field = Json.getString(spec, "field");
        return new Path(field);
    }

    private static String asStringOrNull(Object v) {
        return v instanceof String ? (String) v : null;
    }

    /** 宽松相等：数字按数值比较，其余用 equals（字符串/布尔严格类型）。 */
    private static boolean looseEquals(Object actual, Object expected) {
        if (actual == null || actual == Json.NULL || expected == null || expected == Json.NULL) {
            return false;
        }
        if (actual instanceof Number && expected instanceof Number) {
            return ((Number) actual).doubleValue() == ((Number) expected).doubleValue();
        }
        return actual.equals(expected);
    }

    private enum Compare {
        EQ("eq"), NE("ne"), GT("gt"), GTE("gte"), LT("lt"), LTE("lte");
        final String token;

        Compare(String token) {
            this.token = token;
        }
    }

    /**
     * 点分字段路径（如 {@code amount} 或 {@code user.tier}）。
     * {@link #resolve(Map)} 在任一段缺失/类型不符时返回 null（视为字段缺失）。
     */
    static final class Path {
        private final String[] segments;

        Path(String dotted) {
            if (dotted == null || dotted.isEmpty()) {
                throw new RuleException("field 不能为空");
            }
            this.segments = dotted.split("\\.", -1);
            for (String s : segments) {
                if (s.isEmpty()) {
                    throw new RuleException("非法字段路径: " + dotted);
                }
            }
        }

        @SuppressWarnings("unchecked")
        Object resolve(Map<String, Object> root) {
            Object cur = root;
            for (String seg : segments) {
                if (!(cur instanceof Map)) {
                    return null;
                }
                cur = ((Map<String, Object>) cur).get(seg);
                if (cur == null) {
                    return null;
                }
            }
            return cur;
        }
    }

    /** 解析入口的安全包装，把 JSON 层异常转成规则异常。 */
    public static Predicate compileQuiet(Object spec) {
        try {
            return compile(Json.asObject(spec));
        } catch (JsonException e) {
            throw new RuleException("规则谓词类型错误: " + e.getMessage());
        }
    }
}
