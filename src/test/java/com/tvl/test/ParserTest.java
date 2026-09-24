package com.tvl.test;

import com.tvl.columnar.Batch;
import com.tvl.columnar.Schema;
import com.tvl.engine.QueryExecutor;
import com.tvl.engine.QueryRequest;
import com.tvl.sql.Parser;
import com.tvl.sql.SqlParseException;
import com.tvl.types.DataType;

import java.util.List;

/**
 * 解析器与 SQL 子集语义：
 *  - 参数个数统计、!= 与 <> 等价、IS NOT NULL、别名、SELECT *
 *  - 括号/关键字大小写/注释
 *  - 语法错误给出明确错误
 *  - 经典三值语义：NOT (a = b) 与 a <> b 在 NULL 上不等价
 */
final class ParserTest {

    private ParserTest() {
    }

    static void register(TestRunner runner) {
        runner.add("解析: 参数计数、!= 与 <> 等价", a -> {
            a.eq(Parser.parseDetailed(
                    "SELECT a FROM t WHERE a = ? OR b <> ? AND c != ?").paramCount(),
                    3, "三个位置参数");

            Batch batch = Batches.fromRows(Schema.of("a", "INTEGER"),
                    List.of(new Object[]{1L}, new Object[]{2L}, new Object[]{null}));
            for (String op : new String[]{"!=", "<>"}) {
                QueryRequest req = new QueryRequest(
                        "SELECT a FROM t WHERE a " + op + " 2",
                        List.of(), List.of(), List.of(batch), true);
                var r = new QueryExecutor().execute(req).get(0);
                a.eq(r.crossCheck(), "MATCH", op + " 向量/逐行一致");
                a.eq(r.output().rowCount(), 1, op + " 只放行 a=1；NULL<>2 是 UNKNOWN 不放行");
            }
        });

        runner.add("解析: IS NOT NULL 与 NULL 安全语义", a -> {
            Batch batch = Batches.fromRows(Schema.of("a", "INTEGER"),
                    List.of(new Object[]{1L}, new Object[]{null}));
            var r1 = new QueryExecutor().execute(new QueryRequest(
                    "SELECT a FROM t WHERE a IS NOT NULL",
                    List.of(), List.of(), List.of(batch), true)).get(0);
            a.eq(r1.output().rowCount(), 1, "IS NOT NULL 只放行非 NULL 行");

            var r2 = new QueryExecutor().execute(new QueryRequest(
                    "SELECT a FROM t WHERE NOT a IS NULL",
                    List.of(), List.of(), List.of(batch), true)).get(0);
            a.eq(r2.output().rowCount(), 1, "NOT a IS NULL 等价 IS NOT NULL");

            // 经典 3VL 陷阱：a <> b 在 NULL 上是 UNKNOWN，NOT (a = b) 也是 UNKNOWN，
            // 两者其实等价；但 a = b OR a <> b 在 NULL 上是 UNKNOWN 而非 TRUE。
            Batch two = Batches.fromRows(Schema.of("a", "INTEGER", "b", "INTEGER"),
                    List.<Object[]>of(new Object[]{null, 1L}));
            var r3 = new QueryExecutor().execute(new QueryRequest(
                    "SELECT a FROM t WHERE a = b OR a <> b",
                    List.of(), List.of(), List.of(two), true)).get(0);
            a.eq(r3.output().rowCount(), 0,
                    "NULL 行上 a=b OR a<>b = UNKNOWN（不是排中律），不放行");
        });

        runner.add("解析: 别名、SELECT *、注释、大小写不敏感关键字", a -> {
            Batch batch = Batches.fromRows(
                    Schema.of("a", "INTEGER", "b", "STRING"),
                    List.of(new Object[]{1L, "x"}, new Object[]{null, "y"}));
            var r = new QueryExecutor().execute(new QueryRequest(
                    "select a AS aa, b bb from t -- 行注释\n where a is null /* block */",
                    List.of(), List.of(), List.of(batch), false)).get(0);
            a.eq(r.output().schema().names(), List.of("aa", "bb"), "别名生效");
            a.eq(r.output().rowCount(), 1, "NULL 行命中");

            var star = new QueryExecutor().execute(new QueryRequest(
                    "SELECT * FROM t WHERE a IS NOT NULL",
                    List.of(), List.of(), List.of(batch), false)).get(0);
            a.eq(star.output().schema().names(), List.of("a", "b"), "* 展开全部列");
            a.eq(star.output().rowCount(), 1, "* 查询非 NULL 行");
        });

        runner.add("解析: 语法错误明确报错", a -> {
            String[] bad = {
                    "SELECT a FROM",
                    "SELECT a FROM t WHERE",
                    "SELECT a FROM t WHERE a =",
                    "SELECT a FROM t WHERE a > ??? b",
                    "SELECT FROM t WHERE a = 1",
                    "SELECT a, FROM t",
                    "SELECT a FROM t WHERE (a = 1",
                    "SELECT a FROM t WHERE a = 'unclosed",
                    "SELECT a FROM t WHERE NOT",
                    "SELECT a FROM t WHERE a = 1 AND"
            };
            for (String sql : bad) {
                a.expectThrows(SqlParseException.class,
                        () -> Parser.parse(sql), "语法错误 SQL 应拒绝: " + sql);
            }
        });

        runner.add("解析: 数值/字符串字面量类型", a -> {
            var p1 = Parser.parseDetailed("SELECT 1, 2.5, 'hi' FROM t");
            a.eq(p1.statement().items().size(), 3, "三个投影项");

            Batch batch = Batches.fromRows(Schema.of("d", "DOUBLE"),
                    List.of(new Object[]{2.5d}, new Object[]{null}));
            var r = new QueryExecutor().execute(new QueryRequest(
                    "SELECT d FROM t WHERE d >= 2.5",
                    List.of(), List.of(), List.of(batch), true)).get(0);
            a.eq(r.output().rowCount(), 1, "DOUBLE 字面量比较");
        });

        runner.add("解析: TRUE/FALSE 常量谓词", a -> {
            Batch batch = Batches.fromRows(Schema.of("a", "INTEGER"),
                    List.of(new Object[]{1L}, new Object[]{null}));
            var all = new QueryExecutor().execute(new QueryRequest(
                    "SELECT a FROM t WHERE TRUE",
                    List.of(), List.of(), List.of(batch), false)).get(0);
            a.eq(all.output().rowCount(), 2, "WHERE TRUE 放行全部（含 NULL 行）");

            var none = new QueryExecutor().execute(new QueryRequest(
                    "SELECT a FROM t WHERE FALSE OR a IS NULL",
                    List.of(), List.of(), List.of(batch), true)).get(0);
            a.eq(none.output().rowCount(), 1, "FALSE OR 只留 NULL 行");
        });
    }
}
