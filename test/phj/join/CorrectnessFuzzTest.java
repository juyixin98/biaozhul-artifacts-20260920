package phj.join;

import phj.Test;
import phj.TestRunner;
import phj.core.JoinType;
import phj.core.QueryRequest;
import phj.core.QueryResult;
import phj.core.Relation;

import java.util.ArrayList;
import java.util.List;

/**
 * 随机模糊测试（固定种子，可复现）。
 * 覆盖矩阵：INNER/LEFT × 内存/落盘/热点/倾斜 × 不同阈值/分区数/NULL 比例/列宽。
 * 每一例都用嵌套循环参考实现做多重集比对。
 */
public class CorrectnessFuzzTest {

    private int casesExecuted;

    @Test
    public void fuzzMatrixAgainstNestedLoop() {
        long seed0 = 20260923L;
        JoinType[] types = {JoinType.INNER, JoinType.LEFT};
        int[] leftSizes = {0, 1, 3, 25, 120};
        int[] rightSizes = {0, 1, 4, 30, 100};
        int[] distinctKeys = {1, 2, 7, 500};
        int[] nullPcts = {0, 15, 60, 100};
        long[] thresholds = {1, 3, 8};
        int[] partitionsCfg = {0, 2, 5};
        int caseNo = 0;

        // 为控制运行时间，取确定性抽样而非全笛卡尔积
        java.util.Random sampler = new java.util.Random(seed0);
        int totalCases = 260;
        for (int c = 0; c < totalCases; c++) {
            JoinType type = types[sampler.nextInt(types.length)];
            int ln = leftSizes[sampler.nextInt(leftSizes.length)];
            int rn = rightSizes[sampler.nextInt(rightSizes.length)];
            int dk = distinctKeys[sampler.nextInt(distinctKeys.length)];
            int np = nullPcts[sampler.nextInt(nullPcts.length)];
            long th = thresholds[sampler.nextInt(thresholds.length)];
            int parts = partitionsCfg[sampler.nextInt(partitionsCfg.length)];
            boolean mixed = sampler.nextBoolean();
            int payload = sampler.nextInt(4) == 0 ? 0 : sampler.nextInt(6); // 部分用例无负载列

            long seed = seed0 + c * 1009L;
            Relation l = TestUtil.randomRelation("L", ln, dk, np, new java.util.Random(seed), mixed, payload);
            Relation r = TestUtil.randomRelation("R", rn, dk, np, new java.util.Random(seed + 1), mixed, payload);

            QueryRequest req = TestUtil.request(l, r, type, List.of("k"), th, parts, -1);
            QueryResult qr = TestUtil.assertAgainstReference(req);
            caseNo++;

            // 额外不变量：INNER 输出行右列宽与左列宽都正确；LEFT 行数 >= 左表行数
            if (type == JoinType.LEFT) {
                TestRunner.assertTrue(qr.rows.size() >= ln,
                        "LEFT 输出行数不应少于左表行数：case=" + caseNo);
            }
            for (var row : qr.rows) {
                TestRunner.assertEquals(l.width() + r.width(), row.width());
            }
        }
        casesExecuted = caseNo;
        System.out.println("    [fuzz] 完成 " + caseNo + " 个随机用例（含空表/全NULL/全热点）");
    }

    @Test
    public void fuzzLargeHotKeyWithNulls() {
        // 重点场景：大单键热点 + NULL 混入，阈值=1 逼出热点回退，两表体量较大
        int totalCases = 12;
        for (int c = 0; c < totalCases; c++) {
            long seed = 7700 + c;
            java.util.Random rnd = new java.util.Random(seed);
            int ln = 200 + rnd.nextInt(200);
            int rn = 150 + rnd.nextInt(150);
            int nullPct = c % 4 == 0 ? 30 : c % 4;
            for (JoinType type : JoinType.values()) {
                Relation l = TestUtil.randomRelation("L", ln, 1, nullPct,
                        new java.util.Random(seed), false, 4);
                Relation r = TestUtil.randomRelation("R", rn, 1, nullPct,
                        new java.util.Random(seed + 1), false, 4);
                QueryRequest req = TestUtil.request(l, r, type, List.of("k"), 2, 4, -1);
                QueryResult qr = TestUtil.assertAgainstReference(req);
                TestRunner.assertTrue((Long) qr.stats.get("hotKeyFallbacks") >= 1,
                        "全热点键应触发回退，case=" + c);
            }
        }
        System.out.println("    [fuzz] 完成 " + totalCases + " x2 个全热点用例");
    }

    @Test
    public void fuzzManyThresholdsPartitions() {
        // 固定数据，扫一遍阈值与分区数组合，结果多重集必须始终一致
        Relation l = TestUtil.randomRelation("L", 220, 33, 12, new java.util.Random(99), true, 2);
        Relation r = TestUtil.randomRelation("R", 180, 33, 12, new java.util.Random(100), true, 2);
        int n = 0;
        for (JoinType type : JoinType.values()) {
            for (long th : new long[]{1, 2, 5, 11, 500}) {
                for (int parts : new int[]{0, 1, 2, 3, 8, 37}) {
                    QueryRequest req = TestUtil.request(l, r, type, List.of("k"), th, parts, -1);
                    TestUtil.assertAgainstReference(req);
                    n++;
                }
            }
        }
        System.out.println("    [fuzz] 阈值x分区矩阵完成 " + n + " 例");
    }

    @Test
    public void fuzzMultiColumnKeys() {
        // 多列键（2 列），含 NULL 与跨型
        int n = 0;
        for (JoinType type : JoinType.values()) {
            for (int t = 0; t < 20; t++) {
                java.util.Random rnd = new java.util.Random(5000 + t);
                Relation l = twoColRel("L", 60, 8, 20, rnd);
                Relation r = twoColRel("R", 50, 8, 20, new java.util.Random(6000 + t));
                QueryRequest req = new QueryRequest();
                req.joinType = type;
                req.keyPairs = List.of(
                        new phj.core.JoinKeyPair("k1", "k1"),
                        new phj.core.JoinKeyPair("k2", "k2"));
                req.left = l;
                req.right = r;
                req.memoryThresholdRows = 1 + (t % 5);
                req.partitions = 2 + (t % 4);
                req.diskQuotaBytes = -1;
                req.validate();
                TestUtil.assertAgainstReference(req);
                n++;
            }
        }
        System.out.println("    [fuzz] 多列键完成 " + n + " 例");
    }

    private Relation twoColRel(String name, int rows, int dk1, int nullPct, java.util.Random rnd) {
        List<String> cols = List.of("id", "k1", "k2", "p");
        List<Object[]> data = new ArrayList<>();
        for (int i = 0; i < rows; i++) {
            Object k1 = rnd.nextInt(100) < nullPct ? null : (long) rnd.nextInt(dk1);
            Object k2;
            int roll = rnd.nextInt(100);
            if (roll < nullPct) k2 = null;
            else if (roll < nullPct + 15) k2 = "s" + rnd.nextInt(dk1); // 字符串类
            else k2 = (long) rnd.nextInt(dk1);
            data.add(new Object[]{(long) i, k1, k2, name + i});
        }
        return TestUtil.relation(name, cols, data.toArray(Object[][]::new));
    }
}
