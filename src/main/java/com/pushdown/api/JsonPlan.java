package com.pushdown.api;

import com.pushdown.exec.Engine;
import com.pushdown.expr.Expr;
import com.pushdown.plan.Plan;
import com.pushdown.json.Json;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Bidirectional conversion between plan/expression trees and their JSON form. */
public final class JsonPlan {

    private JsonPlan() {}

    // ------------------------------------------------------------ expression

    @SuppressWarnings("unchecked")
    public static Expr exprFromJson(Object o) {
        if (!(o instanceof Map)) throw new IllegalArgumentException("expression must be an object: " + o);
        Map<String, Object> m = (Map<String, Object>) o;
        if (m.containsKey("lit")) return new Expr.Lit(m.get("lit"));
        if (m.containsKey("col")) {
            String s = (String) m.get("col");
            int dot = s.indexOf('.');
            if (dot <= 0 || dot == s.length() - 1)
                throw new IllegalArgumentException("column must be qualified as table.column: " + s);
            return new Expr.Col(s.substring(0, dot), s.substring(dot + 1));
        }
        String op = (String) m.get("op");
        List<Object> args = (List<Object>) m.get("args");
        if (op == null || args == null)
            throw new IllegalArgumentException("expression needs 'lit', 'col' or 'op'+'args': " + Json.write(m));
        return switch (op) {
            case "=", "!=", "<", "<=", ">", ">=", "+", "-", "*" ->
                    new Expr.Bin(op, exprFromJson(args.get(0)), exprFromJson(args.get(1)));
            case "and" -> fold(args, true);
            case "or" -> fold(args, false);
            case "not" -> new Expr.Not(exprFromJson(args.get(0)));
            case "isnull" -> new Expr.IsNull(exprFromJson(args.get(0)));
            case "isnotnull" -> new Expr.IsNotNull(exprFromJson(args.get(0)));
            default -> throw new IllegalArgumentException("unknown operator: " + op);
        };
    }

    private static Expr fold(List<Object> args, boolean and) {
        if (args.isEmpty()) throw new IllegalArgumentException("and/or needs at least one argument");
        Expr acc = exprFromJson(args.get(0));
        for (int i = 1; i < args.size(); i++) {
            Expr next = exprFromJson(args.get(i));
            acc = and ? new Expr.And(acc, next) : new Expr.Or(acc, next);
        }
        return acc;
    }

    public static Object exprToJson(Expr e) {
        Map<String, Object> m = new LinkedHashMap<>();
        switch (e) {
            case Expr.Lit l -> m.put("lit", l.value());
            case Expr.Col c -> m.put("col", c.table() + "." + c.name());
            case Expr.Bin b -> { m.put("op", b.op()); m.put("args", List.of(exprToJson(b.l()), exprToJson(b.r()))); }
            case Expr.And a -> { m.put("op", "and"); m.put("args", List.of(exprToJson(a.l()), exprToJson(a.r()))); }
            case Expr.Or o -> { m.put("op", "or"); m.put("args", List.of(exprToJson(o.l()), exprToJson(o.r()))); }
            case Expr.Not n -> { m.put("op", "not"); m.put("args", List.of(exprToJson(n.e()))); }
            case Expr.IsNull i -> { m.put("op", "isnull"); m.put("args", List.of(exprToJson(i.e()))); }
            case Expr.IsNotNull i -> { m.put("op", "isnotnull"); m.put("args", List.of(exprToJson(i.e()))); }
        }
        return m;
    }

    // ------------------------------------------------------------------ plan

    @SuppressWarnings("unchecked")
    public static Plan planFromJson(Map<String, Object> m) {
        String type = (String) m.get("type");
        if (type == null) throw new IllegalArgumentException("plan node needs a 'type'");
        return switch (type) {
            case "scan" -> new Plan.Scan((String) m.get("table"));
            case "filter" -> new Plan.Filter(
                    exprFromJson(m.get("pred")),
                    planFromJson((Map<String, Object>) m.get("child")));
            case "project" -> {
                List<Map<String, Object>> items = (List<Map<String, Object>>) m.get("items");
                List<Plan.Item> out = new ArrayList<>();
                for (Map<String, Object> it : items)
                    out.add(new Plan.Item(exprFromJson(it.get("expr")), (String) it.get("alias")));
                yield new Plan.Project(out, planFromJson((Map<String, Object>) m.get("child")));
            }
            case "join" -> {
                String jt = (String) m.get("joinType");
                Plan.JoinType type1 = switch (jt == null ? "inner" : jt.toLowerCase()) {
                    case "inner" -> Plan.JoinType.INNER;
                    case "left" -> Plan.JoinType.LEFT;
                    default -> throw new IllegalArgumentException("unknown joinType: " + jt);
                };
                yield new Plan.Join(
                        type1,
                        planFromJson((Map<String, Object>) m.get("left")),
                        planFromJson((Map<String, Object>) m.get("right")),
                        exprFromJson(m.get("on")));
            }
            default -> throw new IllegalArgumentException("unknown plan node type: " + type);
        };
    }

    public static Object planToJson(Plan p) {
        Map<String, Object> m = new LinkedHashMap<>();
        switch (p) {
            case Plan.Scan s -> { m.put("type", "scan"); m.put("table", s.table()); }
            case Plan.Filter f -> {
                m.put("type", "filter");
                m.put("pred", exprToJson(f.pred()));
                m.put("child", planToJson(f.child()));
            }
            case Plan.Project pr -> {
                m.put("type", "project");
                List<Object> items = new ArrayList<>();
                for (Plan.Item it : pr.items()) {
                    Map<String, Object> im = new LinkedHashMap<>();
                    im.put("expr", exprToJson(it.expr()));
                    im.put("alias", it.alias());
                    items.add(im);
                }
                m.put("items", items);
                m.put("child", planToJson(pr.child()));
            }
            case Plan.Join j -> {
                m.put("type", "join");
                m.put("joinType", j.type().name().toLowerCase());
                m.put("left", planToJson(j.left()));
                m.put("right", planToJson(j.right()));
                m.put("on", exprToJson(j.on()));
            }
        }
        return m;
    }

    // ---------------------------------------------------------------- tables

    @SuppressWarnings("unchecked")
    public static Map<String, Engine.Table> tablesFromJson(Map<String, Object> m) {
        Map<String, Engine.Table> out = new LinkedHashMap<>();
        for (Map.Entry<String, Object> e : m.entrySet()) {
            Map<String, Object> t = (Map<String, Object>) e.getValue();
            List<String> cols = new ArrayList<>();
            for (Object c : (List<Object>) t.get("columns")) cols.add((String) c);
            List<Object[]> rows = new ArrayList<>();
            for (Object r : (List<Object>) t.get("rows")) {
                List<Object> cells = (List<Object>) r;
                if (cells.size() != cols.size())
                    throw new IllegalArgumentException("row width " + cells.size()
                            + " does not match columns of table " + e.getKey());
                rows.add(cells.toArray());
            }
            out.put(e.getKey(), new Engine.Table(cols, rows));
        }
        return out;
    }
}
