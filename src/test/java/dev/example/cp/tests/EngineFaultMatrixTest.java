package dev.example.cp.tests;

import dev.example.cp.engine.CountScheduler;
import dev.example.cp.engine.Engine;
import dev.example.cp.fail.CrashPoint;
import dev.example.cp.fail.InjectedCrash;

import java.nio.file.Path;

import static dev.example.cp.tests.TestSupport.FINAL_OFFSET;
import static dev.example.cp.tests.TestSupport.FINAL_SUMS;

/**
 * 验收核心：在检查点事务的<b>每个</b>关键阶段注入一次性故障，
 * 随后重新打开引擎（= 进程重启 + 恢复）并继续，最终偏移与汇总必须等于无故障基线。
 *
 * <p>事件 10 条，CountScheduler(5)：epoch 1 覆盖偏移 0..4，epoch 2 覆盖 5..9。
 * 故障全部注入在 epoch 2，保证恢复路径上既有“已提交的旧 epoch”也有“待补完/作废的新 epoch”。
 */
public class EngineFaultMatrixTest extends TestCase {

    private static final CrashPoint[] POINTS = {
            CrashPoint.PENDING_WRITE,
            CrashPoint.STATE_WRITE,
            CrashPoint.COMMIT_RENAME,
            CrashPoint.TABLE_APPLY,
            CrashPoint.AFTER_COMMIT,
    };

    public EngineFaultMatrixTest() {
        super("fault-matrix/crash at every checkpoint stage then recover == continuous baseline");
    }

    @Override
    protected void run() {
        // 基线（无故障连续执行）
        Path baselineDir = newDataDir();
        TestSupport.seed(baselineDir);
        Engine.Status baseline = TestSupport.runBaseline(baselineDir);
        assertEquals(FINAL_OFFSET, baseline.committedOffset(), "baseline offset");
        assertEquals(FINAL_SUMS, baseline.sums(), "baseline sums");

        for (CrashPoint point : POINTS) {
            scenario(point);
        }
    }

    private void scenario(CrashPoint point) {
        Path dir = newDataDir();
        TestSupport.seed(dir);

        // 1) 先让 epoch 1（偏移 0..4）干净地完成：处理 5 条后 CountScheduler 自动检查点
        Engine e1 = TestSupport.open(dir, new CountScheduler(5));
        e1.runUntilEventCount(5);
        assertEquals(1L, e1.status().appliedEpoch(), point + ": epoch 1 committed before crash");
        assertEquals(4L, e1.status().committedOffset(), point + ": epoch 1 covers offsets 0..4");

        // 2) 在 epoch 2 的指定点安排一次性故障，再处理剩余 5 条
        e1.faults().arm(point, 2L, false);
        InjectedCrash crash = assertThrows(InjectedCrash.class,
                () -> e1.runUntilDrained(), // 处理 5..9，在 epoch 2 检查点处崩溃
                point + ": crash must be injected at epoch 2");
        assertEquals(point, crash.point(), point + ": crash point matches");
        assertEquals(2L, crash.epochId(), point + ": crash epoch matches");

        // 3) 中间现场（崩溃后、恢复前）的不变量：汇总表只能处于“已原子提交”的某个前缀位置，
        //    绝不会出现半个 epoch。注意必须用 StatusReader 只读观察；Engine.open 会执行恢复。
        var reader = new dev.example.cp.engine.StatusReader(dir);
        var midTable = reader.table();
        long expectedMidEpoch = (point == CrashPoint.AFTER_COMMIT) ? 2L : 1L;
        assertEquals(expectedMidEpoch, midTable.appliedEpoch(),
                point + ": table at a committed prefix after crash");
        long expectedMidOffset = (point == CrashPoint.AFTER_COMMIT) ? 9L : 4L;
        assertEquals(expectedMidOffset, midTable.lastConsumedOffset(),
                point + ": committed offset at a prefix boundary after crash");

        // 4) 重新打开引擎 = 恢复；继续排空并补提交，最终必须收敛到基线
        Engine e2 = TestSupport.open(dir, new CountScheduler(5));
        e2.runUntilDrainedAndCommitted();
        Engine.Status after = e2.status();

        assertEquals(FINAL_OFFSET, after.committedOffset(),
                point + ": final committed offset equals continuous baseline");
        assertEquals(FINAL_SUMS, after.sums(),
                point + ": final sums equal continuous baseline (no loss, no duplicate)");
        assertEquals(FINAL_OFFSET, after.lastConsumedOffset(),
                point + ": consumed offset matches committed offset");
        assertEquals(2L, after.appliedEpoch(), point + ": table ends at epoch 2");

        // 5) 再来一轮恢复（无新事件），结果仍然稳定 —— 无重复提交
        Engine e3 = TestSupport.open(dir, new CountScheduler(5));
        e3.runUntilDrainedAndCommitted();
        assertEquals(FINAL_SUMS, e3.status().sums(), point + ": sums stable across second recovery");
        assertEquals(FINAL_OFFSET, e3.status().committedOffset(), point + ": offset stable across second recovery");
        assertEquals(2L, e3.status().appliedEpoch(), point + ": appliedEpoch not double-advanced");
    }
}
