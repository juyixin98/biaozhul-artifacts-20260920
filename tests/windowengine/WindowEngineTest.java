package windowengine;

import windowengine.engine.WindowEngine;
import windowengine.plan.QueryPlan;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

/**
 * 窗口引擎核心测试：
 * 1. 固定边界用例（并列键、单行分区、窗口越界、负值、NULL 顺序、空帧、全 NULL 帧）；
 * 2. 大规模随机差分测试：生产引擎 vs 朴素逐行参考实现，逐行逐列比较。
 */
public final class WindowEngineTest {

    // ---------- 测试辅助 ----------

    private static Map<String, Object> orderEntry(String col, String dir, String nullOrder) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("column", col);
        m.put("direction", dir);
        m.put("nullOrder", nullOrder);
        return m;
    }

    private static Map<String, Object> fn(String fn, String alias) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("function", fn);
        m.put("alias", alias);
        return m;
    }

    private static Map<String, Object> sumFn(String col, String alias) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("function", "SUM");
        m.put("column", col);
        m.put("alias", alias);
        return m;
    }

    private static Map<String, Object> boundObj(String type) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("type", type);
        return m;
    }

    private static Map<String, Object> boundObj(String type, long offset) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("type", type);
        m.put("offset", offset);
        return m;
    }

    private static Map<String, Object> frameObj(Map<String, Object> start, Map<String, Object> end) {
        Map<String, Object> f = new LinkedHashMap<>();
        f.put("mode", "ROWS");
        f.put("start", start);
        f.put("end", end);
        return f;
    }

    private record Executed(Relation result, Map<String, Integer> outputIndex) {
        Value cell(int row, String alias) {
            return result.rows().get(row).get(outputIndex.get(alias.toLowerCase()));
        }
    }

    /** 走“JSON 树 -> RequestParser -> WindowEngine”正式链路。 */
    private static Executed runRequest(Map<String, Object> request) {
        QueryRequest qr = new RequestParser().parse(request);
        Relation result = new WindowEngine().execute(qr);
        Map<String, Integer> idx = new LinkedHashMap<>();
        for (int i = 0; i < result.schema().size(); i++) {
            idx.put(result.schema().name(i).toLowerCase(), i);
        }
        return new Executed(result, idx);
    }

    // ---------- 固定用例 1：ROW_NUMBER / RANK 并列语义 ----------

    public void testRankAndRowNumberTies() {
        // 成绩并列：两个 90 并列第 1，80 排第 3；ROW_NUMBER 必须连续不并列
        TestData d = new TestData()
                .column("id", Value.Type.LONG)
                .column("score", Value.Type.LONG)
                .row(1L, 90L)
                .row(2L, 90L)
                .row(3L, 80L)
                .row(4L, 90L)
                .row(5L, null)
                .row(6L, 70L);

        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of(orderEntry("score", "ASC", "FIRST")));
        Map<String, Object> req = d.toRequest(win,
                List.of(fn("ROW_NUMBER", "rn"), fn("RANK", "rk")));
        Executed ex = runRequest(req);

        // 排序后顺序：NULL(行4) -> 70(行5) -> 80(行2) -> 90(行0) -> 90(行1) -> 90(行3)
        // 按 sourceIndex 回原行检查
        long[] expectRnkBySource = {4L, 4L, 3L, 4L, 1L, 2L};
        long[] expectRnBySource = {4L, 5L, 3L, 6L, 1L, 2L};
        for (int i = 0; i < 6; i++) {
            Asserts.assertEquals(expectRnkBySource[i], ex.cell(i, "rk").asLong(),
                    "RANK 行 " + i);
            Asserts.assertEquals(expectRnBySource[i], ex.cell(i, "rn").asLong(),
                    "ROW_NUMBER 行 " + i);
        }
    }

    // ---------- 固定用例 2：并列打破的确定性（相等键按输入序） ----------

    public void testRowNumberTieBreakIsStableByInputOrder() {
        TestData d = new TestData()
                .column("k", Value.Type.LONG)
                .column("v", Value.Type.LONG)
                .row(5L, 100L)
                .row(5L, 101L)
                .row(5L, 102L);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of(orderEntry("k", "ASC", "FIRST")));
        Map<String, Object> req = d.toRequest(win,
                List.of(fn("ROW_NUMBER", "rn"), fn("RANK", "rk")));
        Executed ex = runRequest(req);
        // 三行键完全相等：RANK 全 1；ROW_NUMBER 必须严格按原始输入顺序 1,2,3
        for (int i = 0; i < 3; i++) {
            Asserts.assertEquals(1L, ex.cell(i, "rk").asLong(), "完全并列时 RANK 全 1，行 " + i);
            Asserts.assertEquals((long) (i + 1), ex.cell(i, "rn").asLong(),
                    "ROW_NUMBER 按输入序，行 " + i);
            Asserts.assertEquals(100L + i, ex.cell(i, "v").asLong(), "输出行序应保持输入序");
        }
    }

    // ---------- 固定用例 3：分区 + 单行分区 ----------

    public void testPartitionAndSingleRowPartition() {
        TestData d = new TestData()
                .column("dept", Value.Type.STRING)
                .column("v", Value.Type.LONG)
                .row("A", 10L)
                .row("B", 99L)
                .row("A", 20L)
                .row("C", 5L)
                .row("A", 30L);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("partitionBy", List.of("dept"));
        win.put("orderBy", List.of(orderEntry("v", "ASC", "FIRST")));
        Map<String, Object> req = d.toRequest(win,
                List.of(fn("ROW_NUMBER", "rn"), fn("RANK", "rk")));
        Executed ex = runRequest(req);
        // 按输入行：A:10,A:20,A:30 与 B/C 各自独立编号
        long[] expectRn = {1L, 1L, 2L, 1L, 3L};
        long[] expectRk = {1L, 1L, 2L, 1L, 3L};
        for (int i = 0; i < 5; i++) {
            Asserts.assertEquals(expectRn[i], ex.cell(i, "rn").asLong(),
                    "分区 ROW_NUMBER 行 " + i);
            Asserts.assertEquals(expectRk[i], ex.cell(i, "rk").asLong(),
                    "无并列时 RANK 等于序号");
        }
    }

    // ---------- 固定用例 4：滑动窗口越界裁剪 ----------

    public void testSlidingFrameOutOfBounds() {
        // ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING，首行无前值、末行无后值
        TestData d = new TestData()
                .column("v", Value.Type.LONG)
                .row(1L).row(2L).row(3L).row(4L).row(5L);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of(orderEntry("v", "ASC", "FIRST")));
        win.put("frame", frameObj(boundObj("PRECEDING", 1), boundObj("FOLLOWING", 1)));
        Map<String, Object> req = d.toRequest(win, List.of(sumFn("v", "s")));
        Executed ex = runRequest(req);
        long[] expect = {3L, 6L, 9L, 12L, 9L};
        for (int i = 0; i < 5; i++) {
            Asserts.assertEquals(expect[i], ex.cell(i, "s").asLong(),
                    "1 PRECEDING..1 FOLLOWING 滑动和 行 " + i);
        }
    }

    // ---------- 固定用例 5：大偏移导致空帧 ----------

    public void testEmptyFrameWhenStartAfterEnd() {
        // ROWS BETWEEN 5 FOLLOWING AND 10 FOLLOWING：5 行分区里帧完全在分区之后 -> 空帧 NULL
        TestData d = new TestData()
                .column("v", Value.Type.LONG)
                .row(1L).row(2L).row(3L);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of(orderEntry("v", "ASC", "FIRST")));
        win.put("frame", frameObj(boundObj("FOLLOWING", 5), boundObj("FOLLOWING", 10)));
        Map<String, Object> req = d.toRequest(win, List.of(sumFn("v", "s")));
        Executed ex = runRequest(req);
        for (int i = 0; i < 3; i++) {
            Asserts.assertTrue(ex.cell(i, "s").isNull(), "帧完全越界应为 NULL，行 " + i);
        }
    }

    // ---------- 固定用例 6：负值求和 ----------

    public void testNegativeValuesSum() {
        TestData d = new TestData()
                .column("v", Value.Type.LONG)
                .row(-10L).row(4L).row(-7L).row(0L).row(13L);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of(orderEntry("v", "ASC", "FIRST")));
        // 默认帧（累计到当前行）
        Map<String, Object> req = d.toRequest(win, List.of(sumFn("v", "cum")));
        Executed ex = runRequest(req);
        // 排序后：-10(s0), -7(s2), 0(s3), 4(s1), 13(s4)
        // 累计和映射回输入行序：s0=-10, s1=(-10-7+0+4)=-13, s2=-17, s3=-17, s4=0
        long[] expect = {-10L, -13L, -17L, -17L, 0L};
        for (int i = 0; i < 5; i++) {
            Asserts.assertEquals(expect[i], ex.cell(i, "cum").asLong(),
                    "负值累计和 行 " + i);
        }
    }

    // ---------- 固定用例 7：NULL 输入忽略 + 全 NULL 帧 ----------

    public void testNullsInSumIgnoredAndAllNullFrame() {
        TestData d = new TestData()
                .column("v", Value.Type.LONG)
                .row((Object) null).row(5L).row((Object) null).row(7L).row((Object) null);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of(orderEntry("v", "ASC", "LAST")));
        win.put("frame", frameObj(boundObj("CURRENT_ROW"), boundObj("CURRENT_ROW")));
        Map<String, Object> req = d.toRequest(win, List.of(sumFn("v", "s")));
        Executed ex = runRequest(req);
        // 排序后三个 NULL 排最后；CURRENT ROW 帧：NULL 行 -> NULL，非 NULL 行 -> 自身值
        // 输入行 0,2,4 为 NULL -> NULL；行1=5；行3=7
        Asserts.assertTrue(ex.cell(0, "s").isNull(), "NULL 行 CURRENT ROW 帧应为 NULL");
        Asserts.assertEquals(5L, ex.cell(1, "s").asLong(), "非 NULL 行帧和为自身");
        Asserts.assertTrue(ex.cell(2, "s").isNull(), "NULL 行 CURRENT ROW 帧应为 NULL");
        Asserts.assertEquals(7L, ex.cell(3, "s").asLong(), "非 NULL 行帧和为自身");
        Asserts.assertTrue(ex.cell(4, "s").isNull(), "NULL 行 CURRENT ROW 帧应为 NULL");
    }

    // ---------- 固定用例 8：DESC + 显式 NULLS FIRST ----------

    public void testDescWithNullsFirst() {
        TestData d = new TestData()
                .column("v", Value.Type.LONG)
                .row(3L).row((Object) null).row(1L).row(3L).row(2L);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of(orderEntry("v", "DESC", "FIRST")));
        Map<String, Object> req = d.toRequest(win,
                List.of(fn("ROW_NUMBER", "rn"), fn("RANK", "rk")));
        Executed ex = runRequest(req);
        // 排序：NULL(source1) -> 3(s0) -> 3(s3) -> 2(s4) -> 1(s2)
        long[] rnBySource = {2L, 1L, 5L, 3L, 4L};
        long[] rkBySource = {2L, 1L, 5L, 2L, 4L};
        for (int i = 0; i < 5; i++) {
            Asserts.assertEquals(rnBySource[i], ex.cell(i, "rn").asLong(),
                    "DESC NULLS FIRST RN 行 " + i);
            Asserts.assertEquals(rkBySource[i], ex.cell(i, "rk").asLong(),
                    "DESC NULLS FIRST RANK 行 " + i);
        }
    }

    // ---------- 固定用例 9：多列排序键上的并列 ----------

    public void testMultiKeyTies() {
        TestData d = new TestData()
                .column("a", Value.Type.LONG)
                .column("b", Value.Type.LONG)
                .row(1L, 1L)
                .row(1L, 1L)
                .row(1L, 2L)
                .row(2L, 1L);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of(
                orderEntry("a", "ASC", "FIRST"),
                orderEntry("b", "ASC", "FIRST")));
        Map<String, Object> req = d.toRequest(win, List.of(fn("RANK", "rk")));
        Executed ex = runRequest(req);
        long[] expect = {1L, 1L, 3L, 4L};
        for (int i = 0; i < 4; i++) {
            Asserts.assertEquals(expect[i], ex.cell(i, "rk").asLong(),
                    "多列键 RANK 行 " + i);
        }
    }

    // ---------- 固定用例 10：空表 ----------

    public void testEmptyTable() {
        TestData d = new TestData()
                .column("v", Value.Type.LONG);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of(orderEntry("v", "ASC", "FIRST")));
        Map<String, Object> req = d.toRequest(win,
                List.of(fn("ROW_NUMBER", "rn"), fn("RANK", "rk"), sumFn("v", "s")));
        Executed ex = runRequest(req);
        Asserts.assertEquals(0, ex.result.rowCount(), "空表结果应为 0 行");
        Asserts.assertEquals(4, ex.result.schema().size(), "输出 schema 仍应追加 3 列");
    }

    // ---------- 固定用例 11：无 ORDER BY 时整个分区一个排名 + 全分区 SUM ----------

    public void testNoOrderByWholePartition() {
        TestData d = new TestData()
                .column("p", Value.Type.LONG)
                .column("v", Value.Type.LONG)
                .row(1L, 5L).row(2L, 7L).row(1L, 3L).row(2L, 1L);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("partitionBy", List.of("p"));
        Map<String, Object> req = d.toRequest(win,
                List.of(fn("RANK", "rk"), sumFn("v", "s")));
        Executed ex = runRequest(req);
        // 无 ORDER BY：RANK 全 1；SUM 默认为整个分区
        long[] sumBySource = {8L, 8L, 8L, 8L};
        for (int i = 0; i < 4; i++) {
            Asserts.assertEquals(1L, ex.cell(i, "rk").asLong(), "无 ORDER BY RANK 恒 1");
            Asserts.assertEquals(sumBySource[i], ex.cell(i, "s").asLong(),
                    "无 ORDER BY SUM 为全分区和");
        }
    }

    // ---------- 固定用例 12：UNBOUNDED FOLLOWING 帧 ----------

    public void testFrameToUnboundedFollowing() {
        TestData d = new TestData()
                .column("v", Value.Type.LONG)
                .row(1L).row(2L).row(3L).row(4L);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of(orderEntry("v", "ASC", "FIRST")));
        win.put("frame", frameObj(boundObj("CURRENT_ROW"), boundObj("UNBOUNDED_FOLLOWING")));
        Map<String, Object> req = d.toRequest(win, List.of(sumFn("v", "s")));
        Executed ex = runRequest(req);
        long[] expect = {10L, 9L, 7L, 4L};
        for (int i = 0; i < 4; i++) {
            Asserts.assertEquals(expect[i], ex.cell(i, "s").asLong(),
                    "CURRENT ROW..UNBOUNDED FOLLOWING 行 " + i);
        }
    }

    // ---------- 固定用例 13：溢出检测 ----------

    public void testOverflowDetected() {
        TestData d = new TestData()
                .column("v", Value.Type.LONG)
                .row(Long.MAX_VALUE).row(1L);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of(orderEntry("v", "ASC", "FIRST")));
        win.put("frame", frameObj(boundObj("UNBOUNDED_PRECEDING"), boundObj("UNBOUNDED_FOLLOWING")));
        Map<String, Object> req = d.toRequest(win, List.of(sumFn("v", "s")));
        try {
            runRequest(req);
            Asserts.fail("MAX+1 应触发 OVERFLOW");
        } catch (EngineException e) {
            Asserts.assertEquals(ErrorCode.OVERFLOW.code(), e.code(), "错误码应为 OVERFLOW");
        }
    }

    public void testNegativeOverflowDetected() {
        TestData d = new TestData()
                .column("v", Value.Type.LONG)
                .row(Long.MIN_VALUE).row(-1L);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of(orderEntry("v", "ASC", "FIRST")));
        win.put("frame", frameObj(boundObj("UNBOUNDED_PRECEDING"), boundObj("UNBOUNDED_FOLLOWING")));
        Map<String, Object> req = d.toRequest(win, List.of(sumFn("v", "s")));
        try {
            runRequest(req);
            Asserts.fail("MIN-1 应触发 OVERFLOW");
        } catch (EngineException e) {
            Asserts.assertEquals(ErrorCode.OVERFLOW.code(), e.code(), "负向溢出也应被捕获");
        }
    }

    // ---------- 固定用例 14：NULL 分区键聚到同一组 ----------

    public void testNullPartitionKeysGroupTogether() {
        TestData d = new TestData()
                .column("p", Value.Type.LONG)
                .column("v", Value.Type.LONG)
                .row(null, 1L).row(null, 2L).row(1L, 9L);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("partitionBy", List.of("p"));
        win.put("orderBy", List.of(orderEntry("v", "ASC", "FIRST")));
        Map<String, Object> req = d.toRequest(win, List.of(fn("ROW_NUMBER", "rn")));
        Executed ex = runRequest(req);
        long[] expect = {1L, 2L, 1L};
        for (int i = 0; i < 3; i++) {
            Asserts.assertEquals(expect[i], ex.cell(i, "rn").asLong(),
                    "NULL 分区键应同组 行 " + i);
        }
    }

    // ---------- 固定用例 15：无分区单列、单行数据（最简场景） ----------

    public void testSingleRowNoPartition() {
        TestData d = new TestData()
                .column("v", Value.Type.LONG)
                .row(42L);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of(orderEntry("v", "ASC", "FIRST")));
        win.put("frame", frameObj(boundObj("PRECEDING", 3), boundObj("FOLLOWING", 3)));
        Map<String, Object> req = d.toRequest(win,
                List.of(fn("ROW_NUMBER", "rn"), fn("RANK", "rk"), sumFn("v", "s")));
        Executed ex = runRequest(req);
        Asserts.assertEquals(1L, ex.cell(0, "rn").asLong(), "单行 RN=1");
        Asserts.assertEquals(1L, ex.cell(0, "rk").asLong(), "单行 RANK=1");
        Asserts.assertEquals(42L, ex.cell(0, "s").asLong(), "大偏移帧裁剪到自身");
    }

    // ---------- 固定用例 16：字符串排序键 + 字典序 ----------

    public void testStringOrderKey() {
        TestData d = new TestData()
                .column("name", Value.Type.STRING)
                .row("banana").row("apple").row("cherry").row("apple");
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of(orderEntry("name", "ASC", "FIRST")));
        Map<String, Object> req = d.toRequest(win,
                List.of(fn("ROW_NUMBER", "rn"), fn("RANK", "rk")));
        Executed ex = runRequest(req);
        // 输入序: banana(s0), apple(s1), cherry(s2), apple(s3)
        long[] rn = {3L, 1L, 4L, 2L};
        long[] rk = {3L, 1L, 4L, 1L};
        for (int i = 0; i < 4; i++) {
            Asserts.assertEquals(rn[i], ex.cell(i, "rn").asLong(), "字符串键 RN 行 " + i);
            Asserts.assertEquals(rk[i], ex.cell(i, "rk").asLong(), "字符串键 RANK 行 " + i);
        }
    }

    // ---------- 固定用例 17：多分区键 ----------

    public void testCompositePartitionKey() {
        TestData d = new TestData()
                .column("country", Value.Type.STRING)
                .column("city", Value.Type.STRING)
                .column("v", Value.Type.LONG)
                .row("CN", "BJ", 5L)
                .row("US", "NY", 9L)
                .row("CN", "SH", 7L)
                .row("CN", "BJ", 2L);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("partitionBy", List.of("country", "city"));
        win.put("orderBy", List.of(orderEntry("v", "ASC", "FIRST")));
        Map<String, Object> req = d.toRequest(win, List.of(fn("ROW_NUMBER", "rn")));
        Executed ex = runRequest(req);
        // CN/BJ 组内按 v 升序：s3(v2) 第1、s0(v5) 第2；US/NY、CN/SH 各单行
        long[] expect = {2L, 1L, 1L, 1L};
        for (int i = 0; i < 4; i++) {
            Asserts.assertEquals(expect[i], ex.cell(i, "rn").asLong(),
                    "复合分区键 RN 行 " + i);
        }
    }

    // ---------- 固定用例 18：起点向前越界到分区之前（空帧） ----------

    public void testFrameEntirelyBeforePartition() {
        TestData d = new TestData()
                .column("v", Value.Type.LONG)
                .row(1L).row(2L);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of(orderEntry("v", "ASC", "FIRST")));
        // 10 PRECEDING .. 5 PRECEDING：任何行的帧都完全落在分区起点之前 -> 空帧
        win.put("frame", frameObj(boundObj("PRECEDING", 10), boundObj("PRECEDING", 5)));
        Map<String, Object> req = d.toRequest(win, List.of(sumFn("v", "s")));
        Executed ex = runRequest(req);
        for (int i = 0; i < 2; i++) {
            Asserts.assertTrue(ex.cell(i, "s").isNull(),
                    "帧完全在分区之前应为空帧 NULL，行 " + i);
        }
    }

    // ---------- 固定用例 19：多函数并存且互不干扰 ----------

    public void testMultipleSumsWithDifferentFrames() {
        // 同一窗口规格下两个 SUM（共享帧）——验证多列结果独立正确
        TestData d = new TestData()
                .column("a", Value.Type.LONG)
                .column("b", Value.Type.LONG)
                .row(1L, 10L).row(2L, 20L).row(3L, 30L);
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("orderBy", List.of(orderEntry("a", "ASC", "FIRST")));
        win.put("frame", frameObj(boundObj("CURRENT_ROW"), boundObj("CURRENT_ROW")));
        Map<String, Object> req = d.toRequest(win,
                List.of(sumFn("a", "sa"), sumFn("b", "sb")));
        Executed ex = runRequest(req);
        long[] ea = {1L, 2L, 3L};
        long[] eb = {10L, 20L, 30L};
        for (int i = 0; i < 3; i++) {
            Asserts.assertEquals(ea[i], ex.cell(i, "sa").asLong(), "CURRENT ROW SUM(a)");
            Asserts.assertEquals(eb[i], ex.cell(i, "sb").asLong(), "CURRENT ROW SUM(b)");
        }
    }

    // =====================================================================
    // 随机差分测试：生产引擎 vs 朴素参考
    // =====================================================================

    public void testRandomDifferentialAgainstNaiveReference() {
        Random rnd = new Random(20260923L);
        int scenarios = 300;
        for (int s = 0; s < scenarios; s++) {
            runOneRandomScenario(rnd, s);
        }
    }

    /**
     * 字符串排序键差分：排序/并列/NULL 逻辑对 STRING 类型同样必须成立。
     * 只比较 ROW_NUMBER / RANK（SUM 参数列仍用 LONG 的 v）。
     */
    public void testRandomDifferentialStringOrderKey() {
        Random rnd = new Random(987654321L);
        String[] words = {"a", "b", "ab", "a", null, "ba", "", "b"};
        for (int scenario = 0; scenario < 120; scenario++) {
            int n = 1 + rnd.nextInt(20);
            TestData d = new TestData()
                    .column("grp", Value.Type.LONG)
                    .column("name", Value.Type.STRING)
                    .column("v", Value.Type.LONG);
            for (int i = 0; i < n; i++) {
                Object grp = (long) rnd.nextInt(3); // 3 个分区
                Object name = words[rnd.nextInt(words.length)];
                Object v = (long) (rnd.nextInt(21) - 10);
                d.row(grp, name, v);
            }
            boolean asc = rnd.nextBoolean();
            boolean nullFirst = rnd.nextBoolean();
            Map<String, Object> win = new LinkedHashMap<>();
            win.put("partitionBy", List.of("grp"));
            win.put("orderBy", List.of(orderEntry("name", asc ? "ASC" : "DESC",
                    nullFirst ? "FIRST" : "LAST")));
            win.put("frame", frameObj(boundObj("PRECEDING", 1), boundObj("FOLLOWING", 1)));
            Map<String, Object> req = d.toRequest(win,
                    List.of(fn("ROW_NUMBER", "rn"), fn("RANK", "rk"), sumFn("v", "s")));
            Executed ex = runRequest(req);

            List<Object[]> naive = NaiveReferenceWindow.of(d.columnNames, d.rows)
                    .partitionBy("grp")
                    .orderBy("name", asc, nullFirst)
                    .frame(-1, 1)
                    .sumColumn("v")
                    .compute();
            for (int i = 0; i < n; i++) {
                String ctx = "字符串场景 " + scenario + " 行 " + i;
                Asserts.assertEquals(naive.get(i)[0], ex.cell(i, "rn").asLong(),
                        ctx + " ROW_NUMBER");
                Asserts.assertEquals(naive.get(i)[1], ex.cell(i, "rk").asLong(),
                        ctx + " RANK");
                if (naive.get(i)[2] == null) {
                    Asserts.assertTrue(ex.cell(i, "s").isNull(), ctx + " SUM NULL");
                } else {
                    Asserts.assertEquals(naive.get(i)[2], ex.cell(i, "s").asLong(),
                            ctx + " SUM");
                }
            }
        }
    }

    private void runOneRandomScenario(Random rnd, int scenarioId) {
        int n = 1 + rnd.nextInt(24); // 1..24 行（含单行分区/空帧的大量机会）

        // 列：p1/p2 分区键（LONG/STRING，可空），o 排序键，v SUM 参数
        TestData d = new TestData()
                .column("p1", Value.Type.LONG)
                .column("p2", Value.Type.LONG)
                .column("o", Value.Type.LONG)
                .column("v", Value.Type.LONG);

        // 控制取值域，制造大量并列 / NULL
        Long[] domain = {-5L, -1L, 0L, 1L, 5L, null};
        for (int i = 0; i < n; i++) {
            Object p1 = maybeNullSmall(rnd);
            Object p2 = rnd.nextInt(3) == 0 ? maybeNullSmall(rnd) : 0L; // p2 常重复
            Object o = pick(domain, rnd);
            Object v;
            int vr = rnd.nextInt(10);
            if (vr == 0) {
                v = null;
            } else if (vr == 1) {
                v = (long) (rnd.nextInt(40) - 20); // 稍大一点的值
            } else {
                v = pick(domain, rnd);
            }
            d.row(p1, p2, o, v);
        }

        boolean hasPartition = rnd.nextInt(4) != 0;
        String[] parts = hasPartition
                ? (rnd.nextBoolean() ? new String[]{"p1"} : new String[]{"p1", "p2"})
                : new String[]{};

        boolean asc = rnd.nextBoolean();
        boolean nullFirst = rnd.nextBoolean();
        Map<String, Object> win = new LinkedHashMap<>();
        win.put("partitionBy", List.of(parts));
        win.put("orderBy", List.of(orderEntry("o", asc ? "ASC" : "DESC",
                nullFirst ? "FIRST" : "LAST")));

        // 随机帧：覆盖各种越界/空帧/无界组合
        int frameKind = rnd.nextInt(8);
        Map<String, Object> startBound;
        Map<String, Object> endBound;
        long refStartDelta;
        long refEndDelta;
        boolean refStartUnbounded = false;
        boolean refEndUnbounded = false;

        switch (frameKind) {
            case 0 -> { // 1 PRECEDING .. 1 FOLLOWING
                startBound = boundObj("PRECEDING", 1);
                endBound = boundObj("FOLLOWING", 1);
                refStartDelta = -1;
                refEndDelta = 1;
            }
            case 1 -> { // 3 PRECEDING .. CURRENT ROW
                startBound = boundObj("PRECEDING", 3);
                endBound = boundObj("CURRENT_ROW");
                refStartDelta = -3;
                refEndDelta = 0;
            }
            case 2 -> { // CURRENT ROW .. 2 FOLLOWING
                startBound = boundObj("CURRENT_ROW");
                endBound = boundObj("FOLLOWING", 2);
                refStartDelta = 0;
                refEndDelta = 2;
            }
            case 3 -> { // UNBOUNDED PRECEDING .. CURRENT ROW（累计）
                startBound = boundObj("UNBOUNDED_PRECEDING");
                endBound = boundObj("CURRENT_ROW");
                refStartUnbounded = true;
                refStartDelta = Long.MIN_VALUE;
                refEndDelta = 0;
            }
            case 4 -> { // CURRENT ROW .. UNBOUNDED FOLLOWING
                startBound = boundObj("CURRENT_ROW");
                endBound = boundObj("UNBOUNDED_FOLLOWING");
                refStartDelta = 0;
                refEndUnbounded = true;
                refEndDelta = Long.MAX_VALUE;
            }
            case 5 -> { // 整个分区
                startBound = boundObj("UNBOUNDED_PRECEDING");
                endBound = boundObj("UNBOUNDED_FOLLOWING");
                refStartUnbounded = true;
                refEndUnbounded = true;
                refStartDelta = Long.MIN_VALUE;
                refEndDelta = Long.MAX_VALUE;
            }
            case 6 -> { // 大偏移，构造大量空帧：5 FOLLOWING .. 8 FOLLOWING
                startBound = boundObj("FOLLOWING", 5);
                endBound = boundObj("FOLLOWING", 8);
                refStartDelta = 5;
                refEndDelta = 8;
            }
            default -> { // 2 PRECEDING .. 4 PRECEDING 非法（起点晚于终点）-> 请求应被拒绝
                Map<String, Object> req = d.toRequest(
                        withFrame(win, frameObj(boundObj("PRECEDING", 2), boundObj("PRECEDING", 4))),
                        List.of(fn("ROW_NUMBER", "rn"), fn("RANK", "rk"), sumFn("v", "s")));
                try {
                    runRequest(req);
                    Asserts.fail("场景 " + scenarioId + "：非法帧 2 PRECEDING..4 PRECEDING 必须报错");
                } catch (EngineException e) {
                    Asserts.assertEquals(ErrorCode.INVALID_FRAME.code(), e.code(),
                            "非法帧错误码");
                }
                return;
            }
        }
        win.put("frame", frameObj(startBound, endBound));

        Map<String, Object> req = d.toRequest(win,
                List.of(fn("ROW_NUMBER", "rn"), fn("RANK", "rk"), sumFn("v", "s")));
        Executed ex = runRequest(req);

        // ---- 朴素参考 ----
        NaiveReferenceWindow ref = NaiveReferenceWindow.of(d.columnNames, d.rows)
                .partitionBy(parts)
                .orderBy("o", asc, nullFirst)
                .sumColumn("v");
        if (refStartUnbounded && refEndUnbounded) {
            ref.frameWholePartition();
        } else if (refStartUnbounded) {
            ref.frameUnboundedTo(refEndDelta);
        } else if (refEndUnbounded) {
            ref.frameFromToUnbounded(refStartDelta);
        } else {
            ref.frame(refStartDelta, refEndDelta);
        }
        List<Object[]> naive = ref.compute();

        // ---- 逐格比较 ----
        Asserts.assertEquals(d.size(), ex.result.rowCount(), "场景 " + scenarioId + " 行数");
        for (int i = 0; i < d.size(); i++) {
            Value rn = ex.cell(i, "rn");
            Value rk = ex.cell(i, "rk");
            Value sm = ex.cell(i, "s");
            Object[] expect = naive.get(i);
            String ctx = "场景 " + scenarioId + " 行 " + i
                    + "（n=" + n + ", asc=" + asc + ", nullFirst=" + nullFirst
                    + ", frame=" + frameKind + "）";
            Asserts.assertEquals(expect[0], rn.asLong(), ctx + " ROW_NUMBER");
            Asserts.assertEquals(expect[1], rk.asLong(), ctx + " RANK");
            if (expect[2] == null) {
                Asserts.assertTrue(sm.isNull(), ctx + " SUM 应为 NULL");
            } else {
                Asserts.assertFalse(sm.isNull(), ctx + " SUM 不应为 NULL");
                Asserts.assertEquals(expect[2], sm.asLong(), ctx + " SUM");
            }
        }
    }

    private static Map<String, Object> withFrame(Map<String, Object> win,
                                                 Map<String, Object> frame) {
        win.put("frame", frame);
        return win;
    }

    private static Object maybeNullSmall(Random rnd) {
        int x = rnd.nextInt(5);
        return x == 0 ? null : (long) (x - 2); // null,-1,0,1,2
    }

    private static Object pick(Long[] values, Random rnd) {
        Object v = values[rnd.nextInt(values.length)];
        return v; // 数组里的 null 元素即 null
    }
}
