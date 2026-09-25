package dev.example.cp.tests;

import dev.example.cp.engine.CountScheduler;
import dev.example.cp.engine.Engine;
import dev.example.cp.fail.CrashPoint;
import dev.example.cp.fail.InjectedCrash;
import dev.example.cp.storage.SourceLog;

import java.nio.file.Path;
import java.util.TreeMap;

import static dev.example.cp.tests.TestSupport.FINAL_OFFSET;

/** 多个 epoch 连续崩溃、同一 epoch 重试再崩溃：恢复始终收敛，无重复提交。 */
public class RepeatedFaultsTest extends TestCase {

    public RepeatedFaultsTest() {
        super("fault-matrix/repeated crashes across and within epochs still converge");
    }

    @Override
    protected void run() {
        stateWriteCrashesInThreeConsecutiveEpochs();
        mixedCrashesWithTableApply();
        doubleCrashAtSameEpoch();
    }

    /**
     * 20 条事件，每 5 条一个 epoch（边界偏移 4/9/14/19）。
     * epoch 2/3/4 都在 STATE_WRITE 点崩溃（状态从不发布 → 每次恢复后该 epoch 被完整重放一次）。
     */
    private void stateWriteCrashesInThreeConsecutiveEpochs() {
        Path dir = newDataDir();
        new SourceLog(dir).appendAll(TestSupport.cycleEvents(20));

        Engine e = TestSupport.open(dir, new CountScheduler(5));
        e.runUntilEventCount(5); // epoch 1 干净完成

        for (long epoch = 2; epoch <= 4; epoch++) {
            e.faults().arm(CrashPoint.STATE_WRITE, epoch, false);
            assertThrows(InjectedCrash.class, e::runUntilNextCheckpoint,
                    "epoch " + epoch + " crashes at STATE_WRITE");
            // 重启：状态停在上一个 epoch，恢复后有界运行恰好重放并完成崩溃的这一个 epoch
            e = TestSupport.open(dir, new CountScheduler(5));
            Engine.RunReport r = e.runUntilNextCheckpoint();
            assertEquals(5L, r.eventsProcessed(), "recovered run replays exactly 5 events");
            assertEquals(epoch, e.status().appliedEpoch(), "epoch " + epoch + " completed after recovery");
        }

        Engine.Status s = e.status();
        assertEquals(19L, s.committedOffset(), "all 20 events committed (offset 0..19)");
        // cycleEvents(20)：a=1+4+7+10+13+16+19=70，b=2+5+8+11+14+17+20=77，c=3+6+9+12+15+18=63
        TreeMap<String, Long> expect = new TreeMap<>();
        expect.put("a", 70L);
        expect.put("b", 77L);
        expect.put("c", 63L);
        assertEquals(expect, s.sums(), "sums after three crashes and recoveries");
        assertEquals(20L, s.processedCount(), "operator processed count is exactly 20 (no replay duplicates)");
    }

    /**
     * 混合链路：epoch 2 崩在 TABLE_APPLY（状态+输出均已发布，仅表未应用 → 恢复本身补应用），
     * epoch 3 崩在 STATE_WRITE（作废重放），随后 epoch 4 正常完成。
     */
    private void mixedCrashesWithTableApply() {
        Path dir = newDataDir();
        new SourceLog(dir).appendAll(TestSupport.cycleEvents(20));

        Engine e = TestSupport.open(dir, new CountScheduler(5));
        e.runUntilEventCount(5);

        // epoch 2：表应用点崩溃
        e.faults().arm(CrashPoint.TABLE_APPLY, 2, false);
        assertThrows(InjectedCrash.class, e::runUntilNextCheckpoint, "epoch 2 crashes at TABLE_APPLY");
        var midReader = new dev.example.cp.engine.StatusReader(dir);
        assertEquals(1L, midReader.table().appliedEpoch(), "table still at epoch 1 right after crash");

        // 恢复即把 epoch 2 的表应用补齐；位置已恢复到 9，后续有界运行直接做 epoch 3
        e = TestSupport.open(dir, new CountScheduler(5));
        assertEquals(2L, e.status().appliedEpoch(), "recovery itself applies epoch 2 to table");
        assertEquals(9L, e.status().lastConsumedOffset(), "consumed position restored to offset 9");

        // epoch 3：状态写入点崩溃
        e.faults().arm(CrashPoint.STATE_WRITE, 3, false);
        assertThrows(InjectedCrash.class, e::runUntilNextCheckpoint, "epoch 3 crashes at STATE_WRITE");

        // 恢复并重放 epoch 3，再完成 epoch 4
        e = TestSupport.open(dir, new CountScheduler(5));
        e.runUntilNextCheckpoint();
        assertEquals(3L, e.status().appliedEpoch(), "epoch 3 completed after replay");
        e.runUntilNextCheckpoint();
        assertEquals(4L, e.status().appliedEpoch(), "epoch 4 completes the chain");
        assertEquals(19L, e.status().committedOffset(), "final offset after mixed crashes");
        assertEquals(20L, e.status().processedCount(), "still exactly 20 processed events");
    }

    /** 同一 epoch 第一次崩在 STATE_WRITE（作废重来），第二次崩在 COMMIT_RENAME（补提交），第三次成功。 */
    private void doubleCrashAtSameEpoch() {
        Path dir = newDataDir();
        TestSupport.seed(dir);

        Engine e1 = TestSupport.open(dir, new CountScheduler(5));
        e1.runUntilEventCount(5);

        // 第一次：状态写入崩溃 → epoch 2 整体作废
        e1.faults().arm(CrashPoint.STATE_WRITE, 2, false);
        assertThrows(InjectedCrash.class, e1::runUntilDrained, "first crash at STATE_WRITE");

        // 第二次：恢复后再次崩溃，这次在输出提交点（状态已发布，pending 待补提交）
        Engine e2 = TestSupport.open(dir, new CountScheduler(5));
        e2.faults().arm(CrashPoint.COMMIT_RENAME, 2, false);
        assertThrows(InjectedCrash.class, e2::runUntilDrained, "second crash at COMMIT_RENAME");

        // 第三次：恢复补提交并完成
        Engine e3 = TestSupport.open(dir, new CountScheduler(5));
        e3.runUntilDrainedAndCommitted();

        assertEquals(FINAL_OFFSET, e3.status().committedOffset(), "offset after double crash");
        assertEquals(TestSupport.FINAL_SUMS, e3.status().sums(), "sums after double crash");
        assertEquals(10L, e3.status().processedCount(), "no duplicate processing");

        // 输出目录里 epoch 2 只能留下一个 committed 文件，不能有 pending/重复产物
        Path outDir = dir.resolve("output").resolve("committing");
        try (var files = java.nio.file.Files.list(outDir)) {
            var names = files.map(p -> p.getFileName().toString()).sorted().toList();
            assertFalse(names.contains("chk-2.pending"), "no leftover pending for epoch 2");
            assertTrue(names.contains("chk-2.committed"), "committed epoch 2 exists exactly once");
            long countCommitted = names.stream().filter(n -> n.endsWith(".committed")).count();
            assertEquals(2L, countCommitted, "exactly two committed epoch files");
        } catch (Exception ex) {
            throw new RuntimeException(ex);
        }
    }
}
