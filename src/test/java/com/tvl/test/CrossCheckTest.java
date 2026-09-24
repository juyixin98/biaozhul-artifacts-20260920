package com.tvl.test;

import com.tvl.columnar.Batch;
import com.tvl.columnar.Schema;
import com.tvl.core.Ternary;
import com.tvl.core.TruthVector;
import com.tvl.engine.QueryExecutor;
import com.tvl.engine.QueryRequest;
import com.tvl.types.DataType;

import java.util.ArrayList;
import java.util.List;

/**
 * 验收核心：枚举 TRUE/FALSE/UNKNOWN 的组合，用逐行解释器对照向量结果。
 *
 * 做法：造两列 a / b，让它们的 (值, NULL 位图) 在不同行上覆盖全部
 * 3×3 = 9 种三值比较结果组合；然后对一批不同形态的谓词（含 IS NULL、
 * 比较、AND/OR/NOT、括号嵌套）分别用向量执行器和逐行解释器求值，
 * 断言每一行的三值都完全一致（不是只比 TRUE/FALSE —— UNKNOWN 也必须一致）。
 */
final class CrossCheckTest {

    private CrossCheckTest() {
    }

    static void register(TestRunner runner) {
        runner.add("穷举对照: 两列 9 种三值组合 x 多种谓词", a -> {
            // a 取值 [1, 2, NULL]，b 取值 [1, 2, NULL]，笛卡尔 9 行。
            // 每个比较谓词在这 9 行上的结果覆盖 TRUE/FALSE/UNKNOWN 全部情形。
            List<Object[]> rows = new ArrayList<>();
            Long[] vals = {1L, 2L, null};
            for (Long x : vals) {
                for (Long y : vals) {
                    rows.add(new Object[]{x, y});
                }
            }
            Schema schema = Schema.of("a", "INTEGER", "b", "INTEGER");
            Batch batch = Batches.fromRows(schema, rows);

            // 前 7 个谓词包含与 NULL 的比较/复合，必然产生 UNKNOWN；
            // 纯 IS NULL 类谓词结果只有 TRUE/FALSE —— 这本身也是三值语义的一部分。
            String[] predicates = {
                    "a = b",
                    "a <> b",
                    "a < b",
                    "a <= b",
                    "a > b",
                    "a >= b",
                    "a = b OR a IS NULL",
                    "a = b AND b IS NOT NULL",
                    "NOT (a = b OR a IS NULL)",
                    "a < b OR a = b",
                    "(a = 1 AND b = 2) OR (a IS NULL AND b IS NOT NULL)",
                    "NOT (NOT a IS NULL)"
            };
            String[] nullProducing = {
                    "a = b", "a <> b", "a < b", "a <= b", "a > b", "a >= b",
                    "a = b OR a IS NULL", "a = b AND b IS NOT NULL",
                    "NOT (a = b OR a IS NULL)", "a < b OR a = b",
                    "(a = 1 AND b = 2) OR (a IS NULL AND b IS NOT NULL)"
            };
            java.util.Set<String> mustSeeUnknown = new java.util.HashSet<>(
                    java.util.Arrays.asList(nullProducing));

            int mismatches = 0;
            for (String pred : predicates) {
                String sql = "SELECT a, b FROM t WHERE " + pred;
                QueryRequest req = new QueryRequest(
                        sql, List.of(), List.of(), List.of(batch), true);
                QueryExecutor exec = new QueryExecutor();
                var results = exec.execute(req);
                String cc = results.get(0).crossCheck();
                if (!"MATCH".equals(cc)) {
                    mismatches++;
                    a.fail("谓词 <" + pred + "> 向量与逐行不一致: " + cc);
                }
                TruthVector tv = results.get(0).truth();
                if (mustSeeUnknown.contains(pred)) {
                    boolean sawUnknown = false;
                    for (int i = 0; i < tv.size(); i++) {
                        if (tv.at(i) == Ternary.UNKNOWN) {
                            sawUnknown = true;
                        }
                    }
                    a.check(sawUnknown, "谓词 <" + pred + "> 在 9 行上至少应出现一次 UNKNOWN");
                }
            }
            a.eq(mismatches, 0, "所有谓词向量/逐行必须一致");
        });

        runner.add("穷举对照: 6 种比较符在三种类型上的全部行结果", a -> {
            // 对每种类型构造 4 行：小于、等于、大于、NULL 比较
            verifyTyped(a, "INTEGER",
                    new Object[]{1L, 2L, 3L, null},
                    new Object[]{2L, 2L, 2L, 2L});
            verifyTyped(a, "DOUBLE",
                    new Object[]{1.5d, 2.0d, 2.5d, null},
                    new Object[]{2.0d, 2.0d, 2.0d, 2.0d});
            verifyTyped(a, "STRING",
                    new Object[]{"apple", "banana", "cherry", null},
                    new Object[]{"banana", "banana", "banana", "banana"});
            verifyTyped(a, "BOOLEAN",
                    new Object[]{false, true, null},
                    new Object[]{true, true, true});
        });

        runner.add("穷举对照: 三个三值变量的括号优先级（3^3=27 行）", a -> {
            // 每个变量独立取三种三值状态：1 -> TRUE（=1 成立），
            // 2 -> FALSE（=1 不成立），NULL -> UNKNOWN（NULL=1）。
            List<Object[]> rows = new ArrayList<>();
            for (int x = 0; x < 3; x++) {
                for (int y = 0; y < 3; y++) {
                    for (int z = 0; z < 3; z++) {
                        rows.add(new Object[]{valueOf(x), valueOf(y), valueOf(z)});
                    }
                }
            }
            Schema schema = Schema.of("x", "INTEGER", "y", "INTEGER", "z", "INTEGER");
            Batch batch = Batches.fromRows(schema, rows);

            // OR 优先级低于 AND 的经典用例；NOT/括号混合。
            String[] sqls = {
                    "SELECT x FROM t WHERE x = 1 OR y = 1 AND z = 1",
                    "SELECT x FROM t WHERE (x = 1 OR y = 1) AND z = 1",
                    "SELECT x FROM t WHERE x = 1 AND y = 1 OR z = 1",
                    "SELECT x FROM t WHERE x = 1 AND (y = 1 OR z = 1)",
                    "SELECT x FROM t WHERE NOT x = 1 AND y = 1 OR z = 1",
                    "SELECT x FROM t WHERE NOT (x = 1 AND y = 1) AND z = 1",
                    "SELECT x FROM t WHERE (NOT x = 1 OR y IS NULL) AND (z = 1 OR z IS NULL)",
                    "SELECT x FROM t WHERE x IS NULL OR (y = 1 AND z = 1)"
            };
            for (String sql : sqls) {
                QueryRequest req = new QueryRequest(
                        sql, List.of(), List.of(), List.of(batch), true);
                var results = new QueryExecutor().execute(req);
                a.eq(results.get(0).crossCheck(), "MATCH",
                        "27 行三态括号用例: " + sql);
            }

            // 显式验证：AND 优先级高于 OR —— 加括号与不加括号必须在某些行不同
            QueryRequest pr = new QueryRequest(
                    "SELECT x FROM t WHERE (x = 1 OR y = 1) AND z = 1",
                    List.of(), List.of(), List.of(batch), false);
            QueryRequest noPr = new QueryRequest(
                    "SELECT x FROM t WHERE x = 1 OR y = 1 AND z = 1",
                    List.of(), List.of(), List.of(batch), false);
            var withParen = new QueryExecutor().execute(pr).get(0).truth();
            var noParen = new QueryExecutor().execute(noPr).get(0).truth();
            int diff = 0;
            for (int i = 0; i < 27; i++) {
                if (withParen.codeAt(i) != noParen.codeAt(i)) {
                    diff++;
                }
            }
            a.check(diff > 0, "括号必须实际改变至少一行的结果（AND 优先级检查），差异行数=" + diff);

            // 找一个确定不同的行：x=TRUE(1), y=FALSE(2), z=UNKNOWN(NULL)
            // 无括号: x OR (y AND z) = TRUE OR UNKNOWN = TRUE
            // 有括号: (x OR y) AND z = TRUE AND UNKNOWN = UNKNOWN
            int found = -1;
            for (int i = 0; i < rows.size(); i++) {
                Object[] r = rows.get(i);
                if (Long.valueOf(1L).equals(r[0]) && Long.valueOf(2L).equals(r[1]) && r[2] == null) {
                    found = i;
                }
            }
            a.check(found >= 0, "定位 (TRUE,FALSE,UNKNOWN) 行");
            if (found >= 0) {
                // 注意状态编码 valueOf: 0->2(FALSE), 1->NULL(UNKNOWN), 2->1(TRUE)
                a.eq(noParen.at(found), Ternary.TRUE,
                        "无括号 TRUE OR (FALSE AND UNKNOWN)=TRUE OR UNKNOWN=TRUE");
                a.eq(withParen.at(found), Ternary.UNKNOWN,
                        "有括号 (TRUE OR FALSE) AND UNKNOWN=TRUE AND UNKNOWN=UNKNOWN");
            }
        });
    }

    /** 三态编码：0 -> 2(FALSE)，1 -> NULL(UNKNOWN)，2 -> 1(TRUE)。 */
    private static Object valueOf(int state) {
        return switch (state) {
            case 0 -> 2L;
            case 1 -> null;
            default -> 1L;
        };
    }

    private static void verifyTyped(Assertions a, String type,
                                    Object[] leftValues, Object[] rightValues) {
        List<Object[]> rows = new ArrayList<>();
        for (int i = 0; i < leftValues.length; i++) {
            rows.add(new Object[]{leftValues[i], rightValues[i]});
        }
        Schema schema = Schema.of("a", type, "b", type);
        Batch batch = Batches.fromRows(schema, rows);
        for (String op : new String[]{"=", "<>", "<", "<=", ">", ">="}) {
            String sql = "SELECT a FROM t WHERE a " + op + " b";
            QueryRequest req = new QueryRequest(
                    sql, List.of(), List.of(), List.of(batch), true);
            var results = new QueryExecutor().execute(req);
            a.eq(results.get(0).crossCheck(), "MATCH",
                    type + " 类型 " + op + " 向量/逐行一致");
            // 末行永远是 a NULL 比较
            a.eq(results.get(0).truth().at(rows.size() - 1), Ternary.UNKNOWN,
                    type + " " + op + " 末行 NULL 必须 UNKNOWN");
        }
    }
}
