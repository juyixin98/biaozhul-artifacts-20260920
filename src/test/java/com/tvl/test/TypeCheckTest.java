package com.tvl.test;

import com.tvl.columnar.Batch;
import com.tvl.columnar.Schema;
import com.tvl.engine.QueryExecutor;
import com.tvl.engine.QueryRequest;
import com.tvl.types.DataType;
import com.tvl.types.TypeCheckException;

import java.util.List;

/**
 * 强类型检查与参数绑定：
 *  - 字符串 "1" 绑定到 INTEGER 参数：拒绝（拒绝隐式字符串数值混转）
 *  - INTEGER 列与字符串字面量比较：拒绝
 *  - 数值跨族（INTEGER/DOUBLE）：允许
 *  - 参数个数不符、参数值为带小数 DOUBLE 绑定 INTEGER：拒绝
 *  - 投影谓词、WHERE 标量：拒绝
 */
final class TypeCheckTest {

    private TypeCheckTest() {
    }

    private static Batch intBatch() {
        return Batches.fromRows(Schema.of("a", "INTEGER", "s", "STRING"),
                List.<Object[]>of(new Object[]{1L, "x"}));
    }

    static void register(TestRunner runner) {
        runner.add("拒绝隐式转换: 字符串参数绑到 INTEGER", a -> {
            QueryRequest req = new QueryRequest(
                    "SELECT a FROM t WHERE a = ?",
                    List.of(DataType.INTEGER), List.of("1"),
                    List.of(intBatch()), false);
            RuntimeException e = a.expectThrows(TypeCheckException.class,
                    () -> new QueryExecutor().execute(req),
                    "\"1\" 不能隐式转为 INTEGER 参数");
            a.check(e != null && e.getMessage().contains("?1") && e.getMessage().contains("INTEGER"),
                    "错误消息需指明参数位置与期望类型: " + (e == null ? "" : e.getMessage()));
        });

        runner.add("拒绝隐式转换: 数字参数绑到 STRING", a -> {
            QueryRequest req = new QueryRequest(
                    "SELECT s FROM t WHERE s = ?",
                    List.of(DataType.STRING), List.of(42L),
                    List.of(intBatch()), false);
            a.expectThrows(TypeCheckException.class,
                    () -> new QueryExecutor().execute(req),
                    "42 不能隐式转为 STRING 参数");
        });

        runner.add("拒绝隐式转换: INTEGER 列 = 字符串字面量", a -> {
            QueryRequest req = new QueryRequest(
                    "SELECT a FROM t WHERE a = '1'",
                    List.of(), List.of(), List.of(intBatch()), false);
            a.expectThrows(TypeCheckException.class,
                    () -> new QueryExecutor().execute(req),
                    "INTEGER = STRING 必须被拒绝");
        });

        runner.add("拒绝隐式转换: STRING 列 > 数字字面量", a -> {
            QueryRequest req = new QueryRequest(
                    "SELECT s FROM t WHERE s > 3",
                    List.of(), List.of(), List.of(intBatch()), false);
            a.expectThrows(TypeCheckException.class,
                    () -> new QueryExecutor().execute(req),
                    "STRING > INTEGER 必须被拒绝");
        });

        runner.add("允许同族数值: INTEGER/DOUBLE 比较与参数", a -> {
            QueryRequest req = new QueryRequest(
                    "SELECT a FROM t WHERE a < ?",
                    List.of(DataType.DOUBLE), List.of(2.0d),
                    List.of(intBatch()), true);
            var results = new QueryExecutor().execute(req);
            a.eq(results.get(0).crossCheck(), "MATCH", "INTEGER < DOUBLE 参数向量/逐行一致");
            a.eq(results.get(0).output().rowCount(), 1, "a=1 < 2.0 命中一行");
        });

        runner.add("参数: 个数不符被拒绝（多/少）", a -> {
            QueryRequest tooMany = new QueryRequest(
                    "SELECT a FROM t WHERE a = ?",
                    List.of(DataType.INTEGER), List.of(1L, 2L),
                    List.of(intBatch()), false);
            a.expectThrows(TypeCheckException.class,
                    () -> new QueryExecutor().execute(tooMany), "参数过多");

            QueryRequest tooFew = new QueryRequest(
                    "SELECT a FROM t WHERE a = ? AND s = ?",
                    List.of(DataType.INTEGER, DataType.STRING), List.of(1L),
                    List.of(intBatch()), false);
            a.expectThrows(TypeCheckException.class,
                    () -> new QueryExecutor().execute(tooFew), "参数过少");

            QueryRequest typesMismatch = new QueryRequest(
                    "SELECT a FROM t WHERE a = ?",
                    List.of(DataType.INTEGER, DataType.STRING), List.of(1L, "x"),
                    List.of(intBatch()), false);
            a.expectThrows(TypeCheckException.class,
                    () -> new QueryExecutor().execute(typesMismatch),
                    "paramTypes 与 params 个数不一致");
        });

        runner.add("参数: DOUBLE 1.5 绑到 INTEGER 被拒绝；3.0 可接受", a -> {
            QueryRequest frac = new QueryRequest(
                    "SELECT a FROM t WHERE a = ?",
                    List.of(DataType.INTEGER), List.of(1.5d),
                    List.of(intBatch()), false);
            a.expectThrows(TypeCheckException.class,
                    () -> new QueryExecutor().execute(frac), "带小数 DOUBLE 绑 INTEGER 拒绝");

            QueryRequest whole = new QueryRequest(
                    "SELECT a FROM t WHERE a = ?",
                    List.of(DataType.INTEGER), List.of(1.0d),
                    List.of(intBatch()), true);
            var rs = new QueryExecutor().execute(whole);
            a.eq(rs.get(0).crossCheck(), "MATCH", "整数值 DOUBLE 绑 INTEGER 正常");
        });

        runner.add("参数: NULL 值绑定合法，比较结果恒 UNKNOWN；IS NULL 可见", a -> {
            QueryRequest req = new QueryRequest(
                    "SELECT a FROM t WHERE a = ?",
                    List.of(DataType.INTEGER), java.util.Arrays.asList((Object) null),
                    List.of(intBatch()), true);
            var r = new QueryExecutor().execute(req).get(0);
            a.eq(r.truth().at(0), com.tvl.core.Ternary.UNKNOWN,
                    "a = NULL 参数恒为 UNKNOWN");
            a.eq(r.output().rowCount(), 0, "UNKNOWN 不放行");
        });

        runner.add("类型检查: WHERE 非布尔标量被拒绝；BOOLEAN 列作谓词合法", a -> {
            QueryRequest scalarWhere = new QueryRequest(
                    "SELECT a FROM t WHERE a",
                    List.of(), List.of(), List.of(intBatch()), false);
            a.expectThrows(TypeCheckException.class,
                    () -> new QueryExecutor().execute(scalarWhere),
                    "WHERE 后裸 INTEGER 列必须类型错误");

            QueryRequest stringWhere = new QueryRequest(
                    "SELECT s FROM t WHERE s",
                    List.of(), List.of(), List.of(intBatch()), false);
            a.expectThrows(TypeCheckException.class,
                    () -> new QueryExecutor().execute(stringWhere),
                    "WHERE 后 STRING 列必须类型错误");

            // BOOLEAN 列直接当谓词：TRUE 放行，FALSE 过滤，NULL -> UNKNOWN 过滤
            Batch boolBatch = Batches.fromRows(
                    com.tvl.columnar.Schema.of("flag", "BOOLEAN"),
                    List.<Object[]>of(
                            new Object[]{Boolean.TRUE},
                            new Object[]{Boolean.FALSE},
                            new Object[]{null}));
            QueryRequest boolWhere = new QueryRequest(
                    "SELECT flag FROM t WHERE flag",
                    List.of(), List.of(), List.of(boolBatch), true);
            var r = new QueryExecutor().execute(boolWhere).get(0);
            a.eq(r.crossCheck(), "MATCH", "BOOLEAN 裸列谓词向量/逐行一致");
            a.eq(r.output().rowCount(), 1, "只有 flag=TRUE 行放行");
        });

        runner.add("类型检查: 投影谓词在解析层被拒绝", a -> {
            QueryRequest predProjection = new QueryRequest(
                    "SELECT a > 2 FROM t",
                    List.of(), List.of(), List.of(intBatch()), false);
            a.expectThrows(com.tvl.sql.SqlParseException.class,
                    () -> new QueryExecutor().execute(predProjection),
                    "投影不支持比较谓词（解析层拒绝）");
        });

        runner.add("类型检查: 裸 NULL 可投影，且与谓词投影区分", a -> {
            var r = new QueryExecutor().execute(new QueryRequest(
                    "SELECT a, NULL AS n FROM t WHERE TRUE",
                    List.of(), List.of(), List.of(intBatch()), false)).get(0);
            a.eq(r.output().schema().typeOf("n"), DataType.BOOLEAN,
                    "裸 NULL 投影列类型兜底 BOOLEAN");
            a.check(r.output().column("n").isNullAt(0), "投影 NULL 值位图");
        });

        runner.add("类型检查: 未知列、未知类型名被拒绝", a -> {
            QueryRequest unknownCol = new QueryRequest(
                    "SELECT nope FROM t",
                    List.of(), List.of(), List.of(intBatch()), false);
            a.expectThrows(TypeCheckException.class,
                    () -> new QueryExecutor().execute(unknownCol), "未知列拒绝");

            // 未知参数类型名在 API 层经 DataType.parse 拒绝
            try {
                DataType.parse("DECIMAL");
                a.fail("DECIMAL 未支持，应抛出非法参数异常");
            } catch (IllegalArgumentException ok) {
                a.check(ok.getMessage().contains("DECIMAL"), "错误消息含类型名");
            }
        });
    }
}
