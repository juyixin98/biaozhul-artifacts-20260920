package vecq;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 把请求 JSON 解析并校验为 {@link QueryPlan}。
 *
 * 校验内容：表存在且列等长；过滤中引用的列存在、比较类型匹配（int/string、
 * 字面量可为 null 但仅 = / !=）；Union 只能位于根；投影 / 聚合 / 分组列存在；
 * 聚合函数合法且类型匹配；显式 selection 的下标合法；batchSize &gt;= 1。
 */
public final class PlanBuilder {

    private final Catalog catalog;

    public PlanBuilder(Catalog catalog) {
        this.catalog = catalog;
    }

    public QueryPlan build(Object requestObj) {
        Map<String, Object> req = Json.asMap(requestObj);

        // 1) 表：目录名引用 或 内联表
        Object tableObj = req.get("table");
        if (tableObj == null) throw new InvalidQueryException("请求缺少 table 字段");
        Table table;
        if (tableObj instanceof String name) {
            table = catalog.require(name);
        } else {
            table = Table.fromJson(tableObj);
        }

        int batchSize = parseBatchSize(req.get("batchSize"));

        // 2) 过滤
        FilterExpr filter = req.containsKey("filter") && req.get("filter") != null
                ? FilterExpr.fromJson(req.get("filter"))
                : null;
        if (filter != null) {
            validateFilter(filter, table, true);
        }

        // 3) 投影
        List<String> projection = parseStringList(req.get("projection"), "projection");

        // 4) 聚合与分组
        List<AggSpec> aggregates = new ArrayList<>();
        if (req.containsKey("aggregates") && req.get("aggregates") != null) {
            for (Object a : Json.asList(req.get("aggregates"))) {
                aggregates.add(AggSpec.fromJson(a));
            }
        }
        List<String> groupBy = parseStringList(req.get("groupBy"), "groupBy");
        validateAggregates(aggregates, groupBy, table);
        for (String g : groupBy) table.column(g); // 存在性检查

        // 5) 显式输入选择向量
        SelectionVector selection = null;
        if (req.containsKey("selection") && req.get("selection") != null) {
            int[] raw = Column.parseIntRows(req.get("selection"), "selection");
            selection = SelectionVector.wrapSorted(table.rowCount(), raw);
        }

        // 6) 导出目录
        Object ed = req.get("exportDir");
        String exportDir = ed == null ? null : String.valueOf(ed);

        return new QueryPlan(table, filter, projection, aggregates, groupBy,
                batchSize, selection, exportDir);
    }

    private int parseBatchSize(Object o) {
        if (o == null) return QueryPlan.DEFAULT_BATCH_SIZE;
        if (!(o instanceof Number n)) {
            throw new InvalidQueryException("batchSize 必须是正整数，实际为 " + Json.typeName(o));
        }
        int bs = n.intValue();
        if (bs < 1) throw new InvalidQueryException("batchSize 必须 >= 1，实际为 " + bs);
        return bs;
    }

    private List<String> parseStringList(Object o, String field) {
        if (o == null) return List.of();
        List<Object> list = Json.asList(o);
        List<String> out = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (Object e : list) {
            if (!(e instanceof String s) || s.isEmpty()) {
                throw new InvalidQueryException(field + " 必须由非空字符串组成");
            }
            if (!seen.add(s)) {
                throw new InvalidQueryException(field + " 中列 \"" + s + "\" 重复出现");
            }
            out.add(s);
        }
        return out;
    }

    // ---------------- 过滤校验 ----------------

    private void validateFilter(FilterExpr f, Table t, boolean atRoot) {
        switch (f) {
            case FilterExpr.Union u -> {
                if (!atRoot) {
                    throw new InvalidQueryException(
                            "union（分支结果合并）只能作为过滤计划的根节点，不能嵌在 and/or/not 内部");
                }
                for (FilterExpr b : u.branches()) {
                    validateFilter(b, t, false);
                    if (b instanceof FilterExpr.Union) {
                        throw new InvalidQueryException("union 的分支不能再嵌套 union");
                    }
                }
            }
            case FilterExpr.And and -> {
                for (FilterExpr c : and.children()) validateFilter(c, t, false);
            }
            case FilterExpr.Or or -> {
                for (FilterExpr c : or.children()) validateFilter(c, t, false);
            }
            case FilterExpr.Not n -> validateFilter(n.child(), t, false);
            case FilterExpr.IsNull isn -> requireColumn(t, isn.column());
            case FilterExpr.Compare cmp -> validateCompare(t, cmp);
        }
    }

    private Column requireColumn(Table t, String name) {
        try {
            return t.column(name);
        } catch (InvalidQueryException e) {
            throw e;
        }
    }

    private void validateCompare(Table t, FilterExpr.Compare cmp) {
        Column c = t.column(cmp.column());
        String op = cmp.op();
        switch (op) {
            case "=", "==", "!=", "<>", "<", "<=", ">", ">=" -> { }
            default -> throw new InvalidQueryException(
                    "不支持的比较算子 \"" + op + "\"（支持 = != < <= > >=）");
        }
        Object v = cmp.value();
        if (v == null) {
            if (!op.equals("=") && !op.equals("==") && !op.equals("!=") && !op.equals("<>")) {
                throw new InvalidQueryException("列 " + cmp.column()
                        + " 与 NULL 只能做 = / != 比较（结果遵循三值逻辑；判空请用 isNull）");
            }
            return; // NULL 字面量对两种列类型都合法
        }
        if (c instanceof IntColumn) {
            if (!(v instanceof Number)) {
                throw new InvalidQueryException("整型列 " + cmp.column()
                        + " 不能与字符串字面量 " + v + " 比较");
            }
            Column.toInt(v); // 范围检查
        } else if (c instanceof StringColumn) {
            if (!(v instanceof String)) {
                throw new InvalidQueryException("字符串列 " + cmp.column()
                        + " 只能与字符串字面量比较，实际为 " + Json.typeName(v));
            }
            if (!op.equals("=") && !op.equals("==") && !op.equals("!=") && !op.equals("<>")) {
                throw new InvalidQueryException("字符串列 " + cmp.column()
                        + " 只支持 = / != 比较，实际为 " + op);
            }
        }
    }

    // ---------------- 聚合校验 ----------------

    private void validateAggregates(List<AggSpec> aggs, List<String> groupBy, Table t) {
        Set<String> outNames = new HashSet<>();
        for (AggSpec a : aggs) {
            String fn = a.func().toLowerCase();
            if (!fn.equals("count") && !fn.equals("sum") && !fn.equals("avg")
                    && !fn.equals("min") && !fn.equals("max")) {
                throw new InvalidQueryException("不支持的聚合函数 \"" + a.func()
                        + "\"（支持 count/sum/avg/min/max）");
            }
            if (!a.isCountStar()) {
                Column c = t.column(a.column());
                if ((fn.equals("sum") || fn.equals("avg")) && !(c instanceof IntColumn)) {
                    throw new InvalidQueryException(fn + " 只能用于整型列，列 " + a.column()
                            + " 是 " + c.typeName() + " 类型");
                }
            }
            if (!outNames.add(a.outputName())) {
                throw new InvalidQueryException("聚合输出名 \"" + a.outputName() + "\" 重复");
            }
        }
        if (!groupBy.isEmpty() && aggs.isEmpty()) {
            throw new InvalidQueryException("指定了 groupBy 但没有任何聚合表达式");
        }
    }
}
