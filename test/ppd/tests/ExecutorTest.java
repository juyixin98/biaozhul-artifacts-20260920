package ppd.tests;

import ppd.*;

import java.util.List;
import java.util.Map;

/** 执行器与 JSON 入口的功能测试。 */
public class ExecutorTest {

    static Table t(String q, List<String> cols, Object[]... rows) {
        List<Row> rs = new java.util.ArrayList<>();
        for (Object[] r : rows) rs.add(new Row(java.util.Arrays.asList(r)));
        return new Table(q, cols, rs);
    }

    static Plan.Scan scan(String name, Table t) {
        return new Plan.Scan(name, t);
    }

    public static void run(Assert a) {
        a.section("执行器：扫描/过滤/投影");
        Table l = t("l", List.of("id", "v"),
                new Object[]{1L, 10L}, new Object[]{2L, 20L}, new Object[]{3L, null});
        Table r = t("r", List.of("id", "w"),
                new Object[]{1L, 100L}, new Object[]{2L, null});

        Plan plan = new Plan.Filter(ExprParser.parse("l.v > 15"), scan("l", l));
        List<Row> rows = new Executor().execute(plan);
        a.check("过滤仅放行 TRUE（UNKNOWN 丢弃），剩 1 行", rows.size() == 1);
        a.check("保留 id=2", rows.get(0).get(0).equals(2L));

        Plan proj = new Plan.Project(
                List.of(new Expr.Ref("l", "id"),
                        ExprParser.parse("l.v + 1")),
                scan("l", l));
        List<Row> pr = new Executor().execute(proj);
        a.check("投影行数不变", pr.size() == 3);
        a.check("算术投影 10+1=11", pr.get(0).get(1).equals(11L));
        a.check("NULL 算术 => NULL", pr.get(2).get(1) == null);

        a.section("执行器：内连接 / 左外连接");
        Plan inner = new Plan.InnerJoin(scan("l", l), scan("r", r),
                List.of(ExprParser.parse("l.id = r.id")));
        List<Row> ij = new Executor().execute(inner);
        a.check("内连接命中 2 行", ij.size() == 2);

        Plan left = new Plan.LeftJoin(scan("l", l), scan("r", r),
                List.of(ExprParser.parse("l.id = r.id")));
        List<Row> lj = new Executor().execute(left);
        a.check("左连接保留全部 3 行", lj.size() == 3);
        a.check("未匹配行右表补 NULL", lj.get(2).get(2) == null && lj.get(2).get(3) == null);
        a.check("右表自身 NULL 值不是补 NULL 行", lj.get(1).get(2).equals(2L));

        // ON 与 WHERE 在左连接上的差别（关键语义）
        Plan onFilter = new Plan.LeftJoin(scan("l", l), scan("r", r),
                List.of(ExprParser.parse("l.id = r.id"),
                        ExprParser.parse("r.w > 150")));
        List<Row> onRows = new Executor().execute(onFilter);
        a.check("ON 中右表过滤不删左行（全保留 3 行）", onRows.size() == 3);

        Plan whereFilter = new Plan.Filter(ExprParser.parse("r.w > 150"),
                new Plan.LeftJoin(scan("l", l), scan("r", r),
                        List.of(ExprParser.parse("l.id = r.id"))));
        List<Row> whereRows = new Executor().execute(whereFilter);
        a.check("WHERE 中右表谓词过滤掉保留行（0 行）", whereRows.isEmpty());

        a.section("JSON 请求入口");
        String req = """
                {
                  "tables": [
                    {"name":"l","columns":["id","v"],"rows":[[1,10],[2,null]]},
                    {"name":"r","columns":["id","w"],"rows":[[1,100]]}
                  ],
                  "plan": {"op":"filter","predicate":"l.v IS NOT NULL",
                    "child":{"op":"join","joinType":"left","on":["l.id = r.id"],
                      "left":{"op":"scan","table":"l"},
                      "right":{"op":"scan","table":"r"}}}
                }
                """;
        QueryEngine.Response resp = new QueryEngine().runJson(req);
        a.check("JSON 请求成功", resp.ok());
        if (resp.ok()) {
            Map<String, Object> res = Json.obj(resp.payload());
            a.check("结果等价标记 true", Boolean.TRUE.equals(res.get("equivalent")));
            a.check("输出 1 行", Integer.valueOf(1).equals(res.get("rowCountAfter")));
        } else {
            a.fail("JSON 请求成功", resp.error());
        }

        // 错误：歧义裸列
        Table dup1 = t("t1", List.of("x"), new Object[]{1L});
        Table dup2 = t("t2", List.of("x"), new Object[]{1L});
        Plan dupJoin = new Plan.InnerJoin(scan("t1", dup1), scan("t2", dup2),
                List.of(ExprParser.parse("t1.x = t2.x")));
        boolean ambig = false;
        try {
            new Executor().execute(new Plan.Filter(ExprParser.parse("x = 1"), dupJoin));
        } catch (EngineException e) {
            ambig = e.getMessage().contains("歧义");
        }
        a.check("重名列裸引用报歧义错误", ambig);
    }
}
