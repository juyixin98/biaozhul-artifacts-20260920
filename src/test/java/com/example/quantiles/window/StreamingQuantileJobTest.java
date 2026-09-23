package com.example.quantiles.window;

import com.example.quantiles.model.Event;
import com.example.quantiles.time.ManualScheduler;
import com.example.quantiles.time.MockClock;
import com.example.quantiles.time.WatermarkGenerator;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 端到端：注入 MockClock + ManualScheduler，按事件时间喂入，
 * 周期性 watermark 触发窗口——整个过程完全确定、无真实线程。
 */
class StreamingQuantileJobTest {

    @Test
    void emitsWindowsWhenInjectedSchedulerFires() {
        WindowSpec spec = new WindowSpec(10, 10, List.of(0.5), 0L);
        List<WindowResult> sink = new ArrayList<>();
        var job = new StreamingQuantileJob(
                spec, WatermarkGenerator.boundedOutOfOrderness(0), sink::add);
        var scheduler = new ManualScheduler();
        job.start(scheduler, new MockClock(0), 5);

        for (long v = 1; v <= 5; v++) {
            job.process(new Event(v, v)); // 值 1..5，时间戳 1..5
        }
        assertTrue(sink.isEmpty(), "定时器未触发前不应产出窗口");

        scheduler.advanceMillis(10); // 周期触发 -> wm=5，窗口未到期
        assertTrue(sink.isEmpty());

        job.process(new Event(12, 100));
        scheduler.advanceMillis(5); // wm=12 -> [0,10) 到期，median 3
        assertEquals(1, sink.size());
        WindowResult w = sink.get(0);
        assertEquals(0, w.windowStartMillis());
        assertEquals(10, w.windowEndMillis());
        assertEquals(5, w.count());
        assertEquals("3", w.values().get(0).toCanonicalString());

        job.stop(scheduler);
    }

    @Test
    void stopsEmittingAfterCancel() {
        WindowSpec spec = new WindowSpec(10, 10, List.of(0.5), 0L);
        var job = new StreamingQuantileJob(
                spec, WatermarkGenerator.boundedOutOfOrderness(0), r -> {
                });
        var scheduler = new ManualScheduler();
        job.start(scheduler, new MockClock(0), 5);
        job.process(new Event(0, 1));
        job.process(new Event(10, 2)); // maxTs=10
        scheduler.advanceMillis(10);  // wm=10 -> [0,10) 到期
        assertEquals(1, job.emittedResults().size());
        job.stop(scheduler);
        scheduler.advanceMillis(100);
        assertEquals(1, job.emittedResults().size());
    }
}
