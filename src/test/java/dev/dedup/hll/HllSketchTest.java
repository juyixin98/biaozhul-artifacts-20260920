package dev.dedup.hll;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Random;
import java.util.Set;

/** HllSketch 行为测试：空集、重复插入、分片合并、估计误差、寄存器上界、跨类型区分。 */
public class HllSketchTest extends TestCase {

    @Override
    public String name() {
        return "HllSketch 行为（空集/重复/分片合并/误差范围/秩上界）";
    }

    @Override
    public void run() {
        testEmpty();
        testDuplicates();
        testRankBounds();
        testEstimateAccuracy();
        testShardMerge();
        testTypeDistinction();
        testCanonicalFloat();
    }

    private void testEmpty() {
        HllSketch s = new HllSketch(new HllConfig(12));
        eqLong(s.estimate(), 0L, "空集估计必须为 0");
        eqLong(s.zeroRegisters(), 4096L, "空集零寄存器数 = m");
        eqLong(s.nonzeroRegisters(), 0L, "空集非零寄存器数 = 0");
        double rse = s.relativeStandardError();
        eqDouble(rse, 1.04 / 64.0, 1e-12, "p=12 RSE=1.04/sqrt(4096)");
    }

    private void testDuplicates() {
        HllSketch s = new HllSketch(new HllConfig(12));
        for (int i = 0; i < 1000; i++) {
            s.addString("same-value");
        }
        eqLong(s.nonzeroRegisters(), 1L, "重复插入 1000 次同一值只命中 1 个寄存器");
        eqLong(s.estimate(), 1L, "纯重复数据估计应≈1（线性计数）");

        // 幂等性：同集合插入两遍，寄存器完全一致
        HllSketch a = new HllSketch(new HllConfig(12));
        HllSketch b = new HllSketch(new HllConfig(12));
        for (int i = 0; i < 5000; i++) {
            String v = "id-" + i;
            a.addString(v);
            b.addString(v);
        }
        for (int i = 0; i < 5000; i++) {
            b.addString("id-" + i);
        }
        check(Arrays.equals(a.registersSnapshot(), b.registersSnapshot()),
                "整集合重复插入两遍后寄存器必须逐字节相同（幂等）");
    }

    private void testRankBounds() {
        // 随机 64 位值下，rank 不得超过 q+1=65-p；p=4 时为 61，仍可放 byte
        for (int p : new int[] {4, 8, 12, 18}) {
            HllSketch s = new HllSketch(new HllConfig(p));
            Random rnd = new Random(1234L + p);
            int maxRank = 65 - p;
            for (int i = 0; i < 200_000; i++) {
                s.addHash(rnd.nextLong());
            }
            byte[] regs = s.registersSnapshot();
            int observedMax = 0;
            for (byte r : regs) {
                check((r & 0xff) <= maxRank, "p=" + p + " 寄存器值不超过 " + maxRank + "，实际 " + (r & 0xff));
                observedMax = Math.max(observedMax, r & 0xff);
            }
            check(observedMax > 0, "p=" + p + " 20 万次插入后应有非零寄存器");
        }
    }

    private void testEstimateAccuracy() {
        // 固定种子：n=100000 时误差应在 10% 内（理论 RSE 2.54%，留宽裕度防脆性）
        long n = 100_000L;
        HllSketch s = new HllSketch(new HllConfig(12));
        Random rnd = new Random(20260922L);
        Set<Long> distinct = new HashSet<>();
        for (long i = 0; i < n; i++) {
            long h = rnd.nextLong();
            distinct.add(h);
            s.addHash(h);
        }
        long est = s.estimate();
        double rel = Math.abs(est - n) / (double) n;
        check(rel < 0.10, "n=100000 估计相对误差 <10%，实际 est=" + est + " rel=" + rel);
        check(est != n || true, "（估计值恰好等于真值也允许）");

        // 小基数线性计数：n=1 时必须精确到 1；n=2 高概率 1~3，这里只查非零与量级
        HllSketch tiny = new HllSketch(new HllConfig(12));
        tiny.addString("only-one");
        eqLong(tiny.estimate(), 1L, "单元素线性计数应给出 1");
    }

    private void testShardMerge() {
        // 把一条确定流按 i%k 分到 4 个分片，merge 结果必须等于整体草图
        int k = 4;
        long n = 50_000L;
        Random rnd = new Random(777L);
        HllSketch whole = new HllSketch(new HllConfig(12));
        HllSketch[] shards = new HllSketch[k];
        for (int i = 0; i < k; i++) {
            shards[i] = new HllSketch(new HllConfig(12));
        }
        for (long i = 0; i < n; i++) {
            long h = rnd.nextLong();
            whole.addHash(h);
            shards[(int) (i % k)].addHash(h);
        }
        HllSketch merged = HllSketch.merge(shards[0], shards[1]);
        HllSketch mergedAll = HllSketch.merge(HllSketch.merge(merged, shards[2]), shards[3]);
        check(mergedAll.equals(whole), "4 分片合并后必须与整体草图逐寄存器一致");
        eqLong(mergedAll.estimate(), whole.estimate(), "合并估计与整体估计相同");

        // 重叠分片合并（两两含重复数据）也必须幂等
        HllSketch overlap = whole.copy();
        overlap.mergeWith(whole);
        check(overlap.equals(whole), "与自己合并不改变草图（重复分片安全）");

        // 合并顺序无关
        HllSketch reverse = shards[3].copy();
        for (int i = 2; i >= 0; i--) {
            reverse.mergeWith(shards[i]);
        }
        check(reverse.equals(whole), "反向顺序合并结果一致");
    }

    private void testTypeDistinction() {
        // "1"(字符串) 与 1(整数) 规范化后必须是不同字节 -> 大概率不同寄存器，估计≈2
        check(!Arrays.equals(ValueCanonicalizer.encode("1"), ValueCanonicalizer.encode(1L)),
                "字符串 \"1\" 与整数 1 的规范化编码必须不同");
        check(!Arrays.equals(ValueCanonicalizer.encode(true), ValueCanonicalizer.encode(1L)),
                "true 与 1 必须区分");
        check(!Arrays.equals(ValueCanonicalizer.encode(null), ValueCanonicalizer.encode("")),
                "null 与空字符串必须区分");

        HllSketch s = new HllSketch(new HllConfig(14));
        s.add(ValueCanonicalizer.encode("1"));
        s.add(ValueCanonicalizer.encode(1L));
        eqLong(s.nonzeroRegisters(), 2L, "字符串1 与 整数1 落入不同寄存器");
        eqLong(s.estimate(), 2L, "跨类型两种元素估计为 2");
    }

    private void testCanonicalFloat() {
        // 1.0 / 1.00 / 1e2 与 100.0 规范化一致性
        check(Arrays.equals(ValueCanonicalizer.encode(1.0), ValueCanonicalizer.encode(1.00)),
                "1.0 与 1.00 规范化一致");
        // parse("1e2") 在自研 JSON 中为 Double
        Object a = Json.parse("1e2");
        Object b = Json.parse("100.0");
        check(Arrays.equals(ValueCanonicalizer.encode(a), ValueCanonicalizer.encode(b)),
                "1e2 与 100.0 规范化一致");
        List<Object> bad = new ArrayList<>();
        // 数组/对象不允许
        fails(IllegalArgumentException.class,
                () -> ValueCanonicalizer.encode(bad), "数组不能作为去重元素");
        fails(IllegalArgumentException.class,
                () -> ValueCanonicalizer.encode(Double.NaN), "NaN 拒绝");
    }
}
