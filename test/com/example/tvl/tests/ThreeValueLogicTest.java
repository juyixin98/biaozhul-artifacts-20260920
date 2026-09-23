package com.example.tvl.tests;

import com.example.tvl.engine.Batch;
import com.example.tvl.engine.Column;
import com.example.tvl.engine.ParameterSet;
import com.example.tvl.engine.QueryPlanner;
import com.example.tvl.engine.QueryService;
import com.example.tvl.engine.RowInterpreter;
import com.example.tvl.engine.Schema;
import com.example.tvl.engine.SqlBool;
import com.example.tvl.engine.VectorExecutor;
import com.example.tvl.sql.Ast;
import com.example.tvl.sql.SqlParser;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static com.example.tvl.tests.MiniTest.assertEquals;
import static com.example.tvl.tests.MiniTest.assertTrue;


/**
 * 验收核心：枚举 TRUE/FALSE/UNKNOWN 全部组合，用测试内独立实现的
 * Kleene 三值真值表，同时对照逐行解释器与向量化执行器。
 */
public final class ThreeValueLogicTest {

    // 独立于产品代码的三值编码：0=FALSE 1=TRUE 2=UNKNOWN
    private static final int F = 0, T = 1, U = 2;

    private static int not(int a) {
        return a == T ? F : a == F ? T : U;
    }

    private static int and(int a, int b) {
        if (a == F || b == F) return F;
        if (a == U || b == U) return U;
        return T;
    }

    private static int or(int a, int b) {
        if (a == T || b == T) return T;
        if (a == U || b == U) return U;
        return F;
    }

    /** 用绑定参数构造原子谓词 ? = 1：值 1→TRUE，2→FALSE，null→UNKNOWN。 */
    private static List<Map<String, Object>> bindings(int... states) {
        List<Map<String, Object>> ps = new ArrayList<>();
        for (int s : states) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("type", "INTEGER");
            Object value;
            if (s == T) {
                value = 1L;
            } else if (s == F) {
                value = 2L;
            } else {
                value = null;
            }
            m.put("value", value);
            ps.add(m);
        }
        return ps;
    }

    private static int evalBoth(String whereSql, List<Map<String, Object>> params) {
        Schema schema = new Schema(List.of(
                new Schema.Column("a", com.example.tvl.sql.DataType.INTEGER)));
        // 单例行，a=1；原子谓词全部基于参数，保证三态只由参数决定
        Batch batch = Batch.fromRows(schema, List.of(List.of(1L)));
        QueryService service = new QueryService();
        QueryService.Compiled c = service.compile(
                "SELECT a FROM t WHERE " + whereSql, schema, (List<?>) (List) params);
        int row = RowInterpreter.evalWhere(c.plan().where(), batch, 0, c.params()).code();
        int vec = VectorExecutor.evalWhere(c.plan().where(), batch, c.params())[0];
        assertEquals(row, vec, "逐行与向量结果必须一致：" + whereSql);
        return row;
    }

    public static MiniTest.Suite suite() {
        return MiniTest.suite("三值逻辑穷举：TRUE/FALSE/UNKNOWN 全组合 × 逐行 vs 向量")

                // ---- NOT：3 种 ----
                .test("NOT 真值表", () -> {
                    for (int a : new int[]{F, T, U}) {
                        int got = evalBoth("NOT (? = 1)", bindings(a));
                        assertEquals(not(a), got, "NOT " + a);
                    }
                })

                // ---- AND：3×3 = 9 种 ----
                .test("AND 真值表（9 组合）", () -> {
                    for (int a : new int[]{F, T, U}) {
                        for (int b : new int[]{F, T, U}) {
                            int got = evalBoth("(? = 1) AND (? = 1)", bindings(a, b));
                            assertEquals(and(a, b), got, "AND " + a + "," + b);
                        }
                    }
                })

                // ---- OR：9 种 ----
                .test("OR 真值表（9 组合）", () -> {
                    for (int a : new int[]{F, T, U}) {
                        for (int b : new int[]{F, T, U}) {
                            int got = evalBoth("(? = 1) OR (? = 1)", bindings(a, b));
                            assertEquals(or(a, b), got, "OR " + a + "," + b);
                        }
                    }
                })

                // ---- 三原子混合：3^3 = 27 种，覆盖括号优先级 ----
                .test("p1 OR p2 AND p3 全 27 组合（AND 优先于 OR）", () -> {
                    int bracketedDifferences = 0;
                    for (int a : new int[]{F, T, U}) {
                        for (int b : new int[]{F, T, U}) {
                            for (int d : new int[]{F, T, U}) {
                                int expected = or(a, and(b, d));
                                int got = evalBoth("(? = 1) OR (? = 1) AND (? = 1)",
                                        bindings(a, b, d));
                                assertEquals(expected, got, "OR/AND " + a + b + d);

                                int withBrackets = evalBoth(
                                        "((? = 1) OR (? = 1)) AND (? = 1)", bindings(a, b, d));
                                assertEquals(and(or(a, b), d), withBrackets,
                                        "括号版 " + a + b + d);
                                if (withBrackets != expected) {
                                    bracketedDifferences++;
                                }
                            }
                        }
                    }
                    // 括号必须在部分组合上改变结果，否则优先级测试无意义
                    assertTrue(bracketedDifferences > 0,
                            "括号应在某些组合改变 AND/OR 结果，实际差异组合数=" + bracketedDifferences);
                })

                // ---- NOT 与 AND/OR 组合 + 括号 ----
                .test("NOT(p1 AND p2) 全 9 组合 与 NOT 结合律", () -> {
                    for (int a : new int[]{F, T, U}) {
                        for (int b : new int[]{F, T, U}) {
                            int got = evalBoth("NOT ((? = 1) AND (? = 1))", bindings(a, b));
                            assertEquals(not(and(a, b)), got, "NOT(AND) " + a + b);
                            // 德摩根在三值逻辑下同样成立
                            int dm = evalBoth("(NOT (? = 1)) OR (NOT (? = 1))", bindings(a, b));
                            assertEquals(got, dm, "德摩根 " + a + b);
                        }
                    }
                })

                // ---- IS NULL 不产生 UNKNOWN ----
                .test("IS [NOT] NULL：NULL→TRUE，非 NULL→TRUE/FALSE 确定值", () -> {
                    Schema schema = new Schema(List.of(
                            new Schema.Column("a", com.example.tvl.sql.DataType.INTEGER)));
                    // a 分别为 1、null
                    Batch batch = Batch.fromRows(schema,
                            java.util.Arrays.asList(List.of(1L), java.util.Arrays.asList((Object) null)));
                    QueryService service = new QueryService();
                    QueryService.Compiled c1 = service.compile(
                            "SELECT a FROM t WHERE a IS NULL", schema, List.of());
                    byte[] isNull = VectorExecutor.evalWhere(c1.plan().where(), batch, c1.params());
                    assertEquals(F, isNull[0], "非 NULL 的 IS NULL 为 FALSE");
                    assertEquals(T, isNull[1], "NULL 的 IS NULL 为 TRUE");
                    QueryService.Compiled c2 = service.compile(
                            "SELECT a FROM t WHERE a IS NOT NULL", schema, List.of());
                    byte[] notNull = VectorExecutor.evalWhere(c2.plan().where(), batch, c2.params());
                    assertEquals(T, notNull[0], "IS NOT NULL 为 TRUE");
                    assertEquals(F, notNull[1], "NULL 的 IS NOT NULL 为 FALSE");
                    for (int code : isNull) {
                        assertTrue(code != U, "IS NULL 绝不能是 UNKNOWN");
                    }
                })

                // ---- 比较的 NULL 传播：列×列 小矩阵 ----
                .test("比较 NULL 传播：a=b 枚举 NULL/非NULL 行", () -> {
                    Schema schema = new Schema(List.of(
                            new Schema.Column("a", com.example.tvl.sql.DataType.INTEGER),
                            new Schema.Column("b", com.example.tvl.sql.DataType.INTEGER)));
                    // 行：(1,1)=, (1,2)<>, (1,null)=U, (null,2)=U, (null,null)=U
                    Batch batch = Batch.fromRows(schema, java.util.Arrays.asList(
                            List.of(1L, 1L),
                            List.of(1L, 2L),
                            java.util.Arrays.asList(1L, null),
                            java.util.Arrays.asList(null, 2L),
                            java.util.Arrays.asList(null, null)));
                    QueryService service = new QueryService();
                    QueryService.Compiled c = service.compile(
                            "SELECT a FROM t WHERE a = b", schema, List.of());
                    byte[] vec = VectorExecutor.evalWhere(c.plan().where(), batch, c.params());
                    int[] expected = {T, F, U, U, U};
                    for (int r = 0; r < expected.length; r++) {
                        int row = RowInterpreter.evalWhere(c.plan().where(), batch, r, c.params()).code();
                        assertEquals(expected[r], vec[r], "向量 第" + r + "行");
                        assertEquals(expected[r], row, "逐行 第" + r + "行");
                    }
                });
    }

    private ThreeValueLogicTest() {}
}
