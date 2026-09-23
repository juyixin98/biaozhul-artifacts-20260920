package ppd;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 关系代数计划树（不可变）。
 *
 * <pre>
 *   plan := Scan(table)
 *         | Filter(predicate, child)
 *         | Project(items, child)            items 至少保持列来源追踪
 *         | InnerJoin(left, right, onPredicates)
 *         | LeftJoin(left, right, onPredicates)   左表为保留侧
 * </pre>
 *
 * schema 由结构推导；列来源用 {@link Column#origin()} 串联，
 * 投影的简单列引用把底层列来源继续向上带，计算列来源为 null。
 */
public sealed interface Plan permits Plan.Scan, Plan.Filter, Plan.Project,
        Plan.InnerJoin, Plan.LeftJoin {

    Schema schema();

    List<Plan> children();

    String typeName();

    /** 基表扫描。 */
    record Scan(String table, Table tableData) implements Plan {
        @Override
        public Schema schema() { return tableData.schema(); }

        @Override
        public List<Plan> children() { return List.of(); }

        @Override
        public String typeName() { return "Scan"; }
    }

    /** 过滤。predicate 为合取谓词（原样保存，拆分由重写器负责）。 */
    record Filter(Expr predicate, Plan child) implements Plan {
        @Override
        public Schema schema() { return child.schema(); }

        @Override
        public List<Plan> children() { return List.of(child); }

        @Override
        public String typeName() { return "Filter"; }
    }

    /**
     * 投影。item 为输出列表达式；simpleOrigin 非空表示该输出是对底层某列的简单引用
     * （用于列来源追踪与谓词下推）。
     */
    final class Project implements Plan {
        private final List<Expr> items;
        private final Plan child;
        private final Schema schema;

        public Project(List<Expr> items, Plan child) {
            this.items = List.copyOf(items);
            this.child = child;
            List<Column> out = new ArrayList<>();
            int exprSeq = 0;
            int constSeq = 0;
            for (Expr e : items) {
                Column c = columnFor(e, child.schema(), exprSeq, constSeq);
                out.add(c);
                if (e instanceof Expr.Arith) exprSeq++;
                if (e instanceof Expr.Lit) constSeq++;
            }
            this.schema = new Schema(out);
        }

        private static Column columnFor(Expr e, Schema input, int exprSeq, int constSeq) {
            if (e instanceof Expr.Ref r) {
                Column src = input.resolve(r.key());
                if (src == null) {
                    throw new EngineException("投影引用了不存在的列: " + r.key());
                }
                // 输出列的身份沿用底层列（限定名、列名），仅通过 origin 保持来源链；
                // 这样投影之后仍可用 t.col 消歧重名列。
                return new Column(src.qualifier(), src.name(), src.nullable(), src);
            }
            if (e instanceof Expr.Lit l) {
                return new Column(null, constSeq == 0 ? "const" : "const" + (constSeq + 1),
                        l.value() == null, null);
            }
            if (e instanceof Expr.Arith a) {
                boolean nullable = false;
                for (RefKey k : a.refs()) {
                    Column c = input.resolve(k);
                    if (c == null || c.nullable()) { nullable = true; break; }
                }
                return new Column(null, exprSeq == 0 ? "expr" : "expr" + (exprSeq + 1),
                        nullable, null);
            }
            throw new EngineException("投影仅支持列引用、常量与算术表达式，得到: " + e.toSql());
        }

        public List<Expr> items() { return items; }

        public Plan child() { return child; }

        @Override
        public Schema schema() { return schema; }

        @Override
        public List<Plan> children() { return List.of(child); }

        @Override
        public String typeName() { return "Project"; }
    }

    /** 内连接。 */
    record InnerJoin(Plan left, Plan right, List<Expr> onPredicates) implements Plan {
        @Override
        public Schema schema() { return joinSchema(left, right); }

        @Override
        public List<Plan> children() { return List.of(left, right); }

        @Override
        public String typeName() { return "InnerJoin"; }
    }

    /** 左外连接：左表为保留侧，右表列在结果上可能为 NULL。 */
    record LeftJoin(Plan left, Plan right, List<Expr> onPredicates) implements Plan {
        @Override
        public Schema schema() {
            Schema base = joinSchema(left, right);
            // 右表列（可能因不匹配被补 NULL）：全部标记 nullable
            int split = left.schema().size();
            List<Column> cols = new ArrayList<>();
            for (int i = 0; i < base.size(); i++) {
                Column c = base.get(i);
                cols.add(i >= split ? c.withNullable(true) : c);
            }
            return new Schema(cols);
        }

        @Override
        public List<Plan> children() { return List.of(left, right); }

        @Override
        public String typeName() { return "LeftJoin"; }
    }

    private static Schema joinSchema(Plan l, Plan r) {
        List<Column> cols = new ArrayList<>(l.schema().columns());
        cols.addAll(r.schema().columns());
        return new Schema(cols);
    }

    // ------------------------------------------------------------------
    // JSON 计划解析
    //
    // {"op":"filter","predicate":"...","child":{...}}
    // {"op":"project","items":["a","a+b",...],"child":{...}}
    // {"op":"join","joinType":"inner|left","on":["...","..."],"left":{...},"right":{...}}
    // {"op":"scan","table":"t"}
    // ------------------------------------------------------------------

    static Plan fromJson(Map<String, Object> node, Map<String, Table> tables) {
        String op = Json.getStr(node, "op");
        return switch (op) {
            case "scan" -> parseScan(node, tables);
            case "filter" -> {
                Plan child = fromJson(Json.getObj(node, "child"), tables);
                Expr pred = ExprParser.parse(Json.getStr(node, "predicate"));
                yield new Filter(pred, child);
            }
            case "project" -> {
                Plan child = fromJson(Json.getObj(node, "child"), tables);
                List<Expr> items = new ArrayList<>();
                for (Object it : Json.arr(node.get("items"))) {
                    items.add(ExprParser.parse(it.toString()));
                }
                yield new Project(items, child);
            }
            case "join" -> {
                Plan left = fromJson(Json.getObj(node, "left"), tables);
                Plan right = fromJson(Json.getObj(node, "right"), tables);
                List<Expr> ons = new ArrayList<>();
                for (Object it : Json.arr(node.get("on"))) {
                    ons.add(ExprParser.parse(it.toString()));
                }
                String joinType = Json.getStr(node, "joinType");
                if (joinType == null || "inner".equalsIgnoreCase(joinType)) {
                    yield new InnerJoin(left, right, ons);
                }
                if ("left".equalsIgnoreCase(joinType)) {
                    yield new LeftJoin(left, right, ons);
                }
                throw new EngineException("不支持的连接类型: " + joinType);
            }
            default -> throw new EngineException("未知计划节点 op=" + op);
        };
    }

    private static Scan parseScan(Map<String, Object> node, Map<String, Table> tables) {
        String name = Json.getStr(node, "table");
        Table t = tables.get(name);
        if (t == null) throw new EngineException("计划引用了未提供的表: " + name);
        return new Scan(name, t);
    }
}
