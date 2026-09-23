package phj.join;

import phj.Test;
import phj.TestRunner;
import phj.core.JoinType;
import phj.core.QueryRequest;
import phj.core.QueryResult;
import phj.core.Relation;

import java.util.List;

public class DiskQuotaTest {

    private static Relation mkRel(String name, int rows, int distinctKeys, int nullPct, long seed) {
        return TestUtil.randomRelation(name, rows, distinctKeys, nullPct,
                new java.util.Random(seed), false, 5);
    }

    /** 无负载列的窄表（id, k），单字节行，用于精确估算磁盘额度。 */
    private static Relation shortRel(String name, int rows, int distinctKeys, int nullPct, long seed) {
        return TestUtil.randomRelation(name, rows, distinctKeys, nullPct,
                new java.util.Random(seed), false, 0);
    }

    @Test
    public void quotaZeroThrowsOnSpill() {
        Relation l = mkRel("L", 100, 40, 0, 1);
        Relation r = mkRel("R", 100, 40, 0, 2);
        QueryRequest req = TestUtil.request(l, r, JoinType.INNER, List.of("k"), 4, 4, 0);
        expectQuota(req);
    }

    @Test
    public void tinyQuotaThrowsDuringInitialPartitioning() {
        Relation l = mkRel("L", 200, 80, 0, 1);
        Relation r = mkRel("R", 200, 80, 0, 2);
        QueryRequest req = TestUtil.request(l, r, JoinType.INNER, List.of("k"), 8, 16, 128);
        expectQuota(req);
    }

    @Test
    public void quotaExhaustedInRecursiveSpill() {
        // 高基数 + 极小阈值 -> 递归再分区时再次申请额度，给一个只够初轮的额度
        Relation l = mkRel("L", 400, 300, 0, 1);
        Relation r = mkRel("R", 400, 300, 0, 2);
        QueryRequest req = TestUtil.request(l, r, JoinType.INNER, List.of("k"), 2, 4, 4096);
        expectQuota(req);
    }

    @Test
    public void quotaExhaustedInLeftMarker() {
        // 全热点、单分区 LEFT：处理热点分区时 build+probe 两份初始文件同时存活，
        // BNL 在其基础上再申请 n 字节的标记位图。
        // 构造已知行数的窄行，精确计算“标记位图之前”的字节基线：
        //   每行 "[i,0]\n" 6 字节（i<10000），两侧共 2*6*n；标记位图 n 字节。
        // 额度设为基线 + n/2：初始分区写得下，但标记位图差 n/2 字节而失败。
        int n = 3000;
        Relation l = shortRel("L", n, 1, 0, 1);
        Relation r = shortRel("R", n, 1, 0, 2);
        int lineBytes = 6;
        long base = 2L * lineBytes * n;
        long quota = base + n / 2;

        QueryRequest failReq = TestUtil.request(l, r, JoinType.LEFT, List.of("k"), 16, 1, quota);
        DiskQuotaException ex = expectQuota(failReq);
        TestRunner.assertTrue(ex.usedBytes() <= quota, "已用字节不应超额度");
    }

    private DiskQuotaException expectQuota(QueryRequest req) {
        try {
            new HashJoinEngine(req).execute(row -> { });
            TestRunner.fail("期望抛出 DiskQuotaException");
            return null;
        } catch (DiskQuotaException expected) {
            TestRunner.assertTrue(expected.usedBytes() <= expected.limitBytes(),
                    "异常时已用字节不应超过额度");
            TestRunner.assertTrue(expected.limitBytes() >= 0);
            return expected;
        }
    }

    @Test
    public void quotaDoesNotAffectInMemory() {
        Relation l = mkRel("L", 4, 4, 0, 1);
        Relation r = mkRel("R", 4, 4, 0, 2);
        QueryRequest req = TestUtil.request(l, r, JoinType.INNER, List.of("k"), 100, 4, 0);
        QueryResult qr = TestUtil.assertAgainstReference(req);
        TestRunner.assertEquals(0L, qr.stats.get("spillPeakBytes"));
    }

    @Test
    public void generousQuotaSucceeds() {
        Relation l = mkRel("L", 300, 60, 10, 1);
        Relation r = mkRel("R", 300, 60, 10, 2);
        QueryRequest req = TestUtil.request(l, r, JoinType.LEFT, List.of("k"), 4, 8, 50_000_000);
        TestUtil.assertAgainstReference(req);
    }
}
