package ppd;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 单机内存执行器：对计划树做实际求值。
 *
 * <ul>
 *   <li>Scan   —— 直接返回基表行；</li>
 *   <li>Filter —— 谓词三值逻辑，仅 TRUE 放行（FALSE/UNKNOWN 都丢弃）；</li>
 *   <li>Project—— 逐项求值（包括算术表达式）；</li>
 *   <li>Join   —— 嵌套循环 + 笛卡尔积按 ON 过滤；左连接不匹配补全 NULL 行。</li>
 * </ul>
 */
public class Executor {

    public List<Row> execute(Plan plan) {
        return switch (plan) {
            case Plan.Scan s -> new ArrayList<>(s.tableData().rows());
            case Plan.Filter f -> execFilter(f);
            case Plan.Project p -> execProject(p);
            case Plan.InnerJoin j -> execInner(j);
            case Plan.LeftJoin j -> execLeft(j);
        };
    }

    private List<Row> execFilter(Plan.Filter f) {
        Schema in = f.child().schema();
        List<Row> rows = execute(f.child());
        List<Row> out = new ArrayList<>();
        for (Row r : rows) {
            Object v = f.predicate().eval(envFor(r, in));
            if (Boolean.TRUE.equals(v)) out.add(r);
        }
        return out;
    }

    private List<Row> execProject(Plan.Project p) {
        Schema in = p.child().schema();
        List<Row> rows = execute(p.child());
        List<Row> out = new ArrayList<>(rows.size());
        for (Row r : rows) {
            Expr.Env env = envFor(r, in);
            Row nr = new Row(p.items().size());
            for (int i = 0; i < p.items().size(); i++) {
                nr.set(i, p.items().get(i).eval(env));
            }
            out.add(nr);
        }
        return out;
    }

    private List<Row> execInner(Plan.InnerJoin j) {
        List<Row> ls = execute(j.left());
        List<Row> rs = execute(j.right());
        Schema lsch = j.left().schema();
        Schema rsch = j.right().schema();
        Schema joined = j.schema();
        List<Row> out = new ArrayList<>();
        for (Row l : ls) {
            for (Row r : rs) {
                Row joinedRow = l.concat(r);
                if (matchAll(j.onPredicates(), joinedRow, joined)) out.add(joinedRow);
            }
        }
        return out;
    }

    private List<Row> execLeft(Plan.LeftJoin j) {
        List<Row> ls = execute(j.left());
        List<Row> rs = execute(j.right());
        Schema rsch = j.right().schema();
        Schema joined = j.schema();
        List<Row> out = new ArrayList<>();
        for (Row l : ls) {
            boolean matched = false;
            for (Row r : rs) {
                Row joinedRow = l.concat(r);
                if (matchAll(j.onPredicates(), joinedRow, joined)) {
                    out.add(joinedRow);
                    matched = true;
                }
            }
            if (!matched) {
                // 保留行：右表列全部补 NULL
                out.add(l.concat(Row.nullRow(rsch.size())));
            }
        }
        return out;
    }

    private boolean matchAll(List<Expr> predicates, Row row, Schema schema) {
        Expr.Env env = envFor(row, schema);
        for (Expr e : predicates) {
            if (!Boolean.TRUE.equals(e.eval(env))) return false;
        }
        return true;
    }

    /**
     * 为一行构造求值环境：把列引用解析到行下标。
     * 限定名精确匹配；裸列名按 schema 解析（歧义由 Schema.resolve 抛错）。
     */
    public static Expr.Env envFor(Row row, Schema schema) {
        Map<RefKey, Integer> qualified = new HashMap<>();
        Map<String, Integer> bare = new HashMap<>();
        for (int i = 0; i < schema.size(); i++) {
            Column c = schema.get(i);
            qualified.put(new RefKey(c.qualifier(), c.name()), i);
            bare.put(c.name(), i);
        }
        return (qualifier, name) -> {
            Integer idx;
            if (qualifier != null) {
                idx = qualified.get(new RefKey(qualifier, name));
            } else {
                Column c = schema.resolve(new RefKey(null, name));
                if (c == null) {
                    throw new EngineException("列不存在: " + name);
                }
                idx = schema.indexOf(c);
            }
            if (idx == null) {
                throw new EngineException("列不存在: " + (qualifier == null ? name : qualifier + "." + name));
            }
            return row.get(idx);
        };
    }
}
