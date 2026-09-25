package dev.example.cp.tests;

import dev.example.cp.core.Event;
import dev.example.cp.engine.Engine;
import dev.example.cp.engine.ManualScheduler;
import dev.example.cp.storage.SourceLog;

import java.nio.file.Path;

import static dev.example.cp.tests.TestSupport.FINAL_OFFSET;
import static dev.example.cp.tests.TestSupport.FINAL_SUMS;

public class EngineHappyPathTest extends TestCase {

    public EngineHappyPathTest() {
        super("engine/no-fault continuous run reaches baseline offset and sums");
    }

    @Override
    protected void run() {
        Path dir = newDataDir();
        TestSupport.seed(dir);

        Engine engine = TestSupport.open(dir, new ManualScheduler());
        Engine.RunReport report = engine.runUntilDrainedAndCommitted();
        assertEquals(10L, report.eventsProcessed(), "all 10 events processed");
        assertEquals(1L, report.checkpointsCompleted(), "one final checkpoint");

        Engine.Status s = engine.status();
        assertEquals(1L, s.latestEpoch(), "one epoch");
        assertEquals(FINAL_OFFSET, s.lastConsumedOffset(), "final consumed offset");
        assertEquals(1L, s.appliedEpoch(), "table applied to epoch 1");
        assertEquals(FINAL_OFFSET, s.committedOffset(), "table committed offset");
        assertEquals(FINAL_SUMS, s.sums(), "final keyed sums");
        assertEquals(10L, s.processedCount(), "operator processed count");

        // 再次打开（无新事件）：状态不应变化，幂等
        Engine reopened = TestSupport.open(dir, new ManualScheduler());
        Engine.RunReport empty = reopened.runUntilDrainedAndCommitted();
        assertEquals(0L, empty.eventsProcessed(), "no new events on reopen");
        assertEquals(FINAL_SUMS, reopened.status().sums(), "sums unchanged on reopen");
        assertEquals(FINAL_OFFSET, reopened.status().committedOffset(), "offset unchanged on reopen");

        // 追加新事件后继续
        new SourceLog(dir).append(new Event(10, "a", 100));
        Engine.RunReport r2 = reopened.runUntilDrainedAndCommitted();
        assertEquals(1L, r2.eventsProcessed(), "one new event processed");
        assertEquals(2L, reopened.status().latestEpoch(), "epoch advances");
        var sums = reopened.status().sums();
        assertEquals(122L, sums.get("a").longValue(), "a sum updated after append");
    }
}
