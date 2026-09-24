package com.tvl.test;

import com.tvl.columnar.Batch;
import com.tvl.columnar.Schema;
import com.tvl.core.Ternary;
import com.tvl.engine.BatchResult;
import com.tvl.engine.QueryExecutor;
import com.tvl.engine.QueryRequest;

import java.util.ArrayList;
import java.util.List;

/**
 * 批次语义：
 *  - 空批次（rows=[]）：输出也是 0 行，不报错，三值向量长度为 0
 *  - 跨批次：参数只绑定一次，schema 必须一致，逐批独立过滤、结果拼接顺序保持
 */
final class BatchTest {

    private BatchTest() {
    }

    static void register(TestRunner runner) {
        runner.add("空批次: rows=[] 正常返回空结果", a -> {
            Schema schema = Schema.of("a", "INTEGER");
            Batch empty = Batches.fromRows(schema, List.of());
            QueryRequest req = new QueryRequest(
                    "SELECT a FROM t WHERE a > 1",
                    List.of(), List.of(), List.of(empty), true);
            BatchResult r = new QueryExecutor().execute(req).get(0);
            a.eq(r.inputRows(), 0, "输入 0 行");
            a.eq(r.truth().size(), 0, "三值向量长度 0");
            a.eq(r.output().rowCount(), 0, "输出 0 行");
            a.eq(r.crossCheck(), "MATCH", "空批次向量/逐行仍应对照一致");
        });

        runner.add("空批次 + 非空批次混合：逐批独立、顺序保持", a -> {
            Schema schema = Schema.of("a", "INTEGER", "s", "STRING");
            Batch b0 = Batches.fromRows(schema, List.of());
            Batch b1 = Batches.fromRows(schema, List.of(
                    new Object[]{1L, "x"},
                    new Object[]{2L, null},
                    new Object[]{null, "z"}));
            Batch b2 = Batches.fromRows(schema, List.of());
            Batch b3 = Batches.fromRows(schema, List.of(
                    new Object[]{5L, "m"},
                    new Object[]{0L, "n"}));
            QueryRequest req = new QueryRequest(
                    "SELECT a, s FROM t WHERE a > 1 OR a IS NULL",
                    List.of(), List.of(), List.of(b0, b1, b2, b3), true);
            List<BatchResult> rs = new QueryExecutor().execute(req);

            a.eq(rs.size(), 4, "输出批次个数保持 4（含两个空批次）");
            a.eq(rs.get(0).output().rowCount(), 0, "批次0 空");
            a.eq(rs.get(1).output().rowCount(), 2, "批次1: a=2 与 a=NULL 命中");
            a.eq(rs.get(2).output().rowCount(), 0, "批次2 空");
            a.eq(rs.get(3).output().rowCount(), 1, "批次3: 只有 a=5 命中");
            for (BatchResult r : rs) {
                a.eq(r.crossCheck(), "MATCH", "每批向量/逐行一致");
            }
            // 三值明细检查（批次1）：a=1 -> FALSE；a=2 -> TRUE；
            // a=NULL: (NULL>1)=UNKNOWN OR (NULL IS NULL)=TRUE -> TRUE
            a.eq(rs.get(1).truth().at(0), Ternary.FALSE, "1>1 = FALSE");
            a.eq(rs.get(1).truth().at(1), Ternary.TRUE, "2>1 = TRUE");
            a.eq(rs.get(1).truth().at(2), Ternary.TRUE, "UNKNOWN OR TRUE(IS NULL) = TRUE");
        });

        runner.add("跨批次: 同一参数对所有批次生效，只绑定一次", a -> {
            Schema schema = Schema.of("a", "INTEGER");
            List<Batch> batches = new ArrayList<>();
            for (int base = 0; base < 5; base++) {
                batches.add(Batches.fromRows(schema, List.of(
                        new Object[]{(long) base},
                        new Object[]{(long) (base + 10)})));
            }
            QueryRequest req = new QueryRequest(
                    "SELECT a FROM t WHERE a >= ?",
                    List.of(com.tvl.types.DataType.INTEGER),
                    List.of(12L), batches, true);
            List<BatchResult> rs = new QueryExecutor().execute(req);
            long selected = 0;
            for (BatchResult r : rs) {
                a.eq(r.crossCheck(), "MATCH", "跨批对照一致");
                selected += r.output().rowCount();
            }
            a.eq(selected, 3L, ">=12 的行: 12,13,14 共 3 行");
        });

        runner.add("跨批次: schema 不一致直接拒绝", a -> {
            Batch b1 = Batches.fromRows(Schema.of("a", "INTEGER"),
                    List.<Object[]>of(new Object[]{1L}));
            Batch b2 = Batches.fromRows(Schema.of("a", "INTEGER", "b", "STRING"),
                    List.<Object[]>of(new Object[]{1L, "x"}));
            QueryRequest req = new QueryRequest(
                    "SELECT a FROM t", List.of(), List.of(), List.of(b1, b2), false);
            RuntimeException e = a.expectThrows(IllegalArgumentException.class,
                    () -> new QueryExecutor().execute(req),
                    "不同 schema 的批次必须被拒绝");
            a.check(e != null && e.getMessage().contains("schema"),
                    "错误消息应指向 schema 不一致");
        });

        runner.add("投影: 常量/参数投影在每批按行数广播，NULL 位图保持", a -> {
            Schema schema = Schema.of("a", "INTEGER");
            Batch batch = Batches.fromRows(schema, List.of(
                    new Object[]{1L}, new Object[]{null}, new Object[]{3L}));
            QueryRequest req = new QueryRequest(
                    "SELECT a, ? AS k FROM t WHERE a IS NULL OR a >= 3",
                    List.of(com.tvl.types.DataType.STRING),
                    List.of("hi"), List.of(batch), false);
            BatchResult r = new QueryExecutor().execute(req).get(0);
            a.eq(r.output().rowCount(), 2, "NULL 行与 a=3 行");
            a.eq(r.output().column(1).type(), com.tvl.types.DataType.STRING,
                    "投影列 k 类型 STRING");
            a.eq(r.output().column(1).strings()[0], "hi", "常量广播第一行");
            a.eq(r.output().column(1).strings()[1], "hi", "常量广播第二行");
            a.check(r.output().column("a").isNullAt(0), "第一行 a 必须是 NULL");
        });
    }
}
