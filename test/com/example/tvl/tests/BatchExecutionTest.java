package com.example.tvl.tests;

import com.example.tvl.engine.Batch;
import com.example.tvl.engine.QueryService;
import com.example.tvl.engine.RowInterpreter;
import com.example.tvl.engine.Schema;
import com.example.tvl.engine.SqlBool;
import com.example.tvl.engine.VectorExecutor;
import com.example.tvl.sql.DataType;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.Random;

import static com.example.tvl.tests.MiniTest.assertEquals;
import static com.example.tvl.tests.MiniTest.assertTrue;


/** 空批次、跨批次独立性，以及随机数据下逐行解释器与向量结果逐行对照。 */
public final class BatchExecutionTest {

    private static Schema schema3() {
        return new Schema(List.of(
                new Schema.Column("id", DataType.INTEGER),
                new Schema.Column("score", DataType.FLOAT),
                new Schema.Column("name", DataType.TEXT)));
    }

    public static MiniTest.Suite suite() {
        QueryService service = new QueryService();
        return MiniTest.suite("空批次 / 跨批次 / 随机逐行对照")

                .test("空批次：0 行返回空结果，三值计数均为 0", () -> {
                    QueryService.Compiled c = service.compile(
                            "SELECT id FROM t WHERE score > 1.0", schema3(), List.of());
                    Batch empty = Batch.fromRows(c.schema(), List.of());
                    Map<String, Object> r = service.executeBatch(c, empty, true);
                    assertEquals(0, r.get("rowCount"), "rowCount");
                    assertEquals(0, r.get("selected"), "selected");
                    assertEquals(0, r.get("trueRows"), "trueRows");
                    assertEquals(0, r.get("falseRows"), "falseRows");
                    assertEquals(0, r.get("unknownRows"), "unknownRows");
                    assertEquals(List.of(), r.get("rows"), "rows");
                    assertEquals(List.of(), r.get("tvl"), "tvl");
                })

                .test("无 WHERE 的空批次也安全", () -> {
                    QueryService.Compiled c = service.compile(
                            "SELECT * FROM t", schema3(), List.of());
                    Map<String, Object> r = service.executeBatch(c, Batch.fromRows(c.schema(), List.of()), true);
                    assertEquals(0, r.get("selected"), "selected");
                })

                .test("跨批次：同一计划连续执行多个批次，结果互不污染且累计正确", () -> {
                    QueryService.Compiled c = service.compile(
                            "SELECT id, name FROM t WHERE score >= ? AND name IS NOT NULL",
                            schema3(), List.of(Map.of("type", "FLOAT", "value", 5.0)));

                    Batch b0 = Batch.fromRows(c.schema(), List.of());
                    Batch b1 = Batch.fromRows(c.schema(), List.of(
                            Arrays.asList(1L, 9.5, "alice"),
                            Arrays.asList(2L, null, "bob"),    // score NULL → UNKNOWN → 排除
                            Arrays.asList(3L, 7.0, null)));   // name NULL → UNKNOWN → 排除
                    Batch b2 = Batch.fromRows(c.schema(), List.of(
                            Arrays.asList(4L, 4.0, "dave"),   // score 小 → FALSE
                            Arrays.asList(5L, 8.25, "erin"),  // TRUE
                            Arrays.asList(null, 6.0, "frank"))); // id NULL 不影响，谓词 TRUE

                    List<Map<String, Object>> rs = service.executeBatches(c, List.of(b0, b1, b2), true);

                    assertEquals(0, rs.get(0).get("selected"), "批次0 选中");
                    assertEquals(1, rs.get(1).get("selected"), "批次1 仅 alice");
                    assertEquals(2, rs.get(2).get("selected"), "批次2 erin 与 frank");

                    // 批次1 三值计数：TRUE=1(alice) FALSE=1(第3行 name NULL 使 AND 为 FALSE) UNKNOWN=1(bob)
                    assertEquals(1, rs.get(1).get("trueRows"), "批次1 TRUE");
                    assertEquals(1, rs.get(1).get("falseRows"), "批次1 FALSE");
                    assertEquals(1, rs.get(1).get("unknownRows"), "批次1 UNKNOWN");

                    // 投影正确性 + NULL 值序列化保留
                    @SuppressWarnings("unchecked")
                    List<List<Object>> rows2 = (List<List<Object>>) rs.get(2).get("rows");
                    assertEquals(List.of(5L, "erin"), rows2.get(0), "erin 行");
                    assertEquals(Arrays.asList(null, "frank"), rows2.get(1), "frank 行 id 为 null");
                })

                .test("跨批次独立：两批交换顺序不改变各自结果", () -> {
                    QueryService.Compiled c = service.compile(
                            "SELECT id FROM t WHERE NOT (id = 1 OR id = 2)",
                            schema3(), List.of());
                    Batch b1 = Batch.fromRows(c.schema(), List.of(
                            Arrays.asList(1L, 1.0, "a"),
                            Arrays.asList(2L, 2.0, "b")));
                    Batch b2 = Batch.fromRows(c.schema(), List.of(
                            Arrays.asList(3L, 3.0, "c")));
                    List<Map<String, Object>> ab = service.executeBatches(c, List.of(b1, b2), false);
                    List<Map<String, Object>> ba = service.executeBatches(c, List.of(b2, b1), false);
                    assertEquals(0, ab.get(0).get("selected"), "b1 无选中");
                    assertEquals(1, ab.get(1).get("selected"), "b2 选中 1");
                    assertEquals(ab.get(0).get("selected"), ba.get(1).get("selected"), "交换后 b1 结果一致");
                    assertEquals(ab.get(1).get("selected"), ba.get(0).get("selected"), "交换后 b2 结果一致");
                })

                .test("随机数据模糊测试：向量与逐行解释器逐行一致（多种表达式）", () -> {
                    String[] predicates = {
                            "id > ?",
                            "score <= ?",
                            "name = ?",
                            "(id > ? AND score < ?) OR name IS NULL",
                            "NOT (id = ? OR score >= ?)",
                            "id IS NOT NULL AND (score > ? OR NOT (name = ?))",
                            "name <> ? AND (id < ? OR score IS NULL)"
                    };
                    Object[][] paramSets = {
                            {Map.of("type", "INTEGER", "value", 50L)},
                            {Map.of("type", "FLOAT", "value", 5.0)},
                            {Map.of("type", "TEXT", "value", "n_20")},
                            {Map.of("type", "INTEGER", "value", 50L), Map.of("type", "FLOAT", "value", 7.5)},
                            {Map.of("type", "INTEGER", "value", 10L), Map.of("type", "FLOAT", "value", 2.0)},
                            {Map.of("type", "FLOAT", "value", 3.0), Map.of("type", "TEXT", "value", "n_7")},
                            {Map.of("type", "TEXT", "value", "n_3"), Map.of("type", "INTEGER", "value", 80L)}
                    };

                    Random rnd = new Random(20260923L);
                    int totalRows = 0;
                    for (int trial = 0; trial < 40; trial++) {
                        String sql = predicates[trial % predicates.length];
                        QueryService.Compiled c = service.compile(
                                "SELECT * FROM t WHERE " + sql, schema3(),
                                List.of(paramSets[trial % predicates.length]));

                        int n = rnd.nextInt(65);
                        List<List<Object>> rows = new ArrayList<>(n);
                        for (int r = 0; r < n; r++) {
                            Object id = rnd.nextInt(5) == 0 ? null : (long) rnd.nextInt(100);
                            Object score = switch (rnd.nextInt(6)) {
                                case 0 -> null;
                                case 1 -> (double) rnd.nextInt(10);
                                default -> rnd.nextInt(100) / 10.0;
                            };
                            Object name = rnd.nextInt(5) == 0 ? null : "n_" + rnd.nextInt(30);
                            rows.add(Arrays.asList(id, score, name));
                        }
                        Batch b = Batch.fromRows(c.schema(), rows);
                        byte[] vec = VectorExecutor.evalWhere(c.plan().where(), b, c.params());
                        for (int r = 0; r < n; r++) {
                            SqlBool row = RowInterpreter.evalWhere(c.plan().where(), b, r, c.params());
                            if (row.code() != vec[r]) {
                                throw new AssertionError(
                                        "trial=" + trial + " row=" + r + " 表达式=" + sql
                                                + " 逐行=" + row + " 向量=" + SqlBool.fromCode(vec[r]));
                            }
                        }
                        totalRows += n;
                    }
                    assertTrue(totalRows > 500, "模糊测试行数应足够多，实际=" + totalRows);
                });
    }
}
