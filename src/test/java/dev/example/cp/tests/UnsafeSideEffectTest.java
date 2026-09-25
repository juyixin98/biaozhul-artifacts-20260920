package dev.example.cp.tests;

import dev.example.cp.engine.CountScheduler;
import dev.example.cp.engine.Engine;
import dev.example.cp.engine.OutputRecord;
import dev.example.cp.fail.CrashPoint;
import dev.example.cp.fail.InjectedCrash;

import java.nio.file.Path;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 适用边界演示：受控本地汇总表做到 exactly-once；
 * 但“在本地事务提交前触发外部副作用”在恢复重试时会重复。
 *
 * <p>这不是协议缺陷的修复对象，而是明确展示保证的<b>边界</b>：
 * 对任意外部副作用，必须由外部系统提供幂等键 / 事务性接口，本检查点协议不做承诺。
 */
public class UnsafeSideEffectTest extends TestCase {

    public UnsafeSideEffectTest() {
        super("boundary/external side effect before commit is duplicated on recovery");
    }

    @Override
    protected void run() {
        Path dir = newDataDir();
        TestSupport.seed(dir);

        // 模拟“外部系统调用次数”（发邮件、扣款、第三方 webhook……）
        AtomicInteger externalCalls = new AtomicInteger(0);
        Engine.UnsafeExternalSideEffect hook = (OutputRecord rec) -> externalCalls.incrementAndGet();

        Engine e1 = TestSupport.open(dir, new CountScheduler(5));
        e1.withUnsafeSideEffect(hook);
        e1.runUntilEventCount(5); // epoch 1 干净提交：hook 触发 1 次
        assertEquals(1, externalCalls.get(), "external side effect fires once for epoch 1");

        // epoch 2 在输出提交前崩溃：hook 已随第一次尝试触发
        e1.faults().arm(CrashPoint.COMMIT_RENAME, 2, false);
        assertThrows(InjectedCrash.class, e1::runUntilDrained, "crash before output commit");

        // 恢复时为了补完 epoch 2 必须重新走提交路径，hook 再触发一次 —— 外部副作用重复
        Engine e2 = TestSupport.open(dir, new CountScheduler(5));
        e2.withUnsafeSideEffect(hook);
        e2.runUntilDrainedAndCommitted();

        assertEquals(2, externalCalls.get(),
                "external side effect fired TWICE (once pre-crash, once on recovery) — not exactly-once");

        // 但本地受控汇总表仍然是 exactly-once：
        assertEquals(9L, e2.status().committedOffset(), "local table offset correct");
        assertEquals(TestSupport.FINAL_SUMS, e2.status().sums(), "local table sums correct, no duplicate");
    }
}
