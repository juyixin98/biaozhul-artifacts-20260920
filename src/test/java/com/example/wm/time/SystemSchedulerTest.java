package com.example.wm.time;

import org.junit.jupiter.api.Test;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 真实调度器冒烟测试（生产注入路径）：只验证周期任务确实被触发、可取消。
 * 不在这里断言精确时间，避免 CI 抖动；精确时序全部由 ManualScheduler 测试覆盖。
 */
class SystemSchedulerTest {

    @Test
    void periodicTaskFires_andCanBeCancelled() throws Exception {
        SystemScheduler scheduler = new SystemScheduler();
        CountDownLatch latch = new CountDownLatch(3);
        var task = scheduler.schedulePeriodic(latch::countDown, 10, 10);
        try {
            assertTrue(latch.await(2, TimeUnit.SECONDS), "周期任务应当被真实调度器触发");
            task.cancel();
            assertTrue(task.isCancelled());
        } finally {
            scheduler.close();
        }
    }
}
