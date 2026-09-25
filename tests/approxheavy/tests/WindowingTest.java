package approxheavy.tests;

import approxheavy.core.ManualClock;
import approxheavy.core.ManualScheduler;
import approxheavy.stream.EventStreamProcessor;

/** Tumbling windows: sealing on events, on scheduler ticks, late rejection, empty windows. */
public final class WindowingTest {
    public static void main(String[] args) {
        TestRunner runner = new TestRunner("WindowingTest");

        runner.add("events land in the window of their timestamp", () -> {
            ManualClock clock = new ManualClock(0);
            EventStreamProcessor p = new EventStreamProcessor(
                    32, 5, 1L, 16, 100, 3, clock, null);
            p.ingest("a", 1, 0);
            p.ingest("a", 1, 50);
            TestRunner.checkEq(p.currentSketch().estimate("a"), 2L, "both in first window");
            TestRunner.checkEq(p.currentWindowStart(), 0L, "window [0,100)");
        });

        runner.add("crossing a boundary seals the old window and opens a new one", () -> {
            ManualClock clock = new ManualClock(0);
            EventStreamProcessor p = new EventStreamProcessor(
                    32, 5, 1L, 16, 100, 3, clock, null);
            p.ingest("a", 1, 10);
            p.ingest("b", 1, 150);
            TestRunner.checkEq(p.currentWindowStart(), 100L, "now in [100,200)");
            TestRunner.checkEq(p.sealedWindows().size(), 1, "one sealed window");
            TestRunner.checkEq(p.sealedWindows().get(0).sketch().totalCount(), 1L,
                    "sealed holds only the first event");
            TestRunner.checkEq(p.currentSketch().estimate("b"), 1L, "new window holds b");
        });

        runner.add("scheduler tick seals windows without new events", () -> {
            ManualClock clock = new ManualClock(0);
            ManualScheduler scheduler = new ManualScheduler();
            EventStreamProcessor p = new EventStreamProcessor(
                    32, 5, 1L, 16, 100, 5, clock, scheduler);
            p.ingest("a", 1, 0);
            clock.setTime(250);
            scheduler.runAllOnce();
            TestRunner.checkEq(p.sealedWindows().size(), 2, "[0,100) and empty [100,200) sealed");
            TestRunner.checkEq(p.currentWindowStart(), 200L, "active is [200,300)");
            TestRunner.checkEq(p.sealedWindows().get(1).sketch().totalCount(), 0L,
                    "empty window materialized");
        });

        runner.add("late events are rejected, not silently dropped", () -> {
            ManualClock clock = new ManualClock(0);
            EventStreamProcessor p = new EventStreamProcessor(
                    32, 5, 1L, 16, 100, 3, clock, null);
            p.ingest("a", 1, 150); // seals [0,100)
            EventStreamProcessor.IngestStatus status = p.ingest("late", 1, 99);
            TestRunner.check(status == EventStreamProcessor.IngestStatus.LATE, "LATE status");
            TestRunner.checkEq(p.sealedWindows().get(0).sketch().estimate("late"), 0L,
                    "late event never counted");
        });

        runner.add("retained windows are bounded (oldest dropped)", () -> {
            ManualClock clock = new ManualClock(0);
            EventStreamProcessor p = new EventStreamProcessor(
                    32, 5, 1L, 16, 10, 2, clock, null);
            // t=0..40 seals [0,10)..[30,40); active window is [40,50).
            for (int t = 0; t < 50; t += 10) {
                p.ingest("k", 1, t);
            }
            TestRunner.checkEq(p.sealedWindows().size(), 2, "only last 2 sealed kept");
            TestRunner.checkEq(p.sealedWindows().get(0).startMillis(), 20L, "oldest dropped");
            TestRunner.checkEq(p.sealedWindows().get(1).startMillis(), 30L, "second kept");
        });

        runner.add("flush force-seals the active window", () -> {
            ManualClock clock = new ManualClock(0);
            EventStreamProcessor p = new EventStreamProcessor(
                    32, 5, 1L, 16, 100, 3, clock, null);
            p.ingest("a", 5, 0);
            p.flush();
            TestRunner.checkEq(p.lastWindow().sketch().estimate("a"), 5L, "sealed");
            TestRunner.checkEq(p.currentSketch().totalCount(), 0L, "fresh window");
        });

        runner.add("mergeWindow combines same-range partitions pointwise", () -> {
            ManualClock clock = new ManualClock(0);
            EventStreamProcessor p1 = new EventStreamProcessor(
                    32, 5, 9L, 16, 100, 3, clock, null);
            EventStreamProcessor p2 = new EventStreamProcessor(
                    32, 5, 9L, 16, 100, 3, clock, null);
            p1.ingest("shared", 10, 0);
            p2.ingest("shared", 4, 0);
            p2.ingest("only2", 2, 0);
            p1.flush();
            p2.flush();
            p1.mergeWindow(p2.lastWindow());
            TestRunner.checkEq(p1.lastWindow().sketch().estimate("shared"), 14L, "shared sums");
            TestRunner.check(p1.lastWindow().candidates().contains("only2"),
                    "candidate union carried over");
        });

        runner.add("mergeWindow rejects partitions with a different seed", () -> {
            ManualClock clock = new ManualClock(0);
            EventStreamProcessor p1 = new EventStreamProcessor(
                    32, 5, 9L, 16, 100, 3, clock, null);
            EventStreamProcessor p2 = new EventStreamProcessor(
                    32, 5, 10L, 16, 100, 3, clock, null);
            p1.ingest("x", 1, 0);
            p2.ingest("y", 1, 0);
            p1.flush();
            p2.flush();
            try {
                p1.mergeWindow(p2.lastWindow());
                throw new AssertionError("incompatible partition merge must fail");
            } catch (approxheavy.cms.IncompatibleSketchException expected) {
                // intended
            }
        });

        runner.add("rangeSketch sums matching sealed windows", () -> {
            ManualClock clock = new ManualClock(0);
            EventStreamProcessor p = new EventStreamProcessor(
                    32, 5, 1L, 16, 100, 5, clock, null);
            p.ingest("a", 3, 0);
            p.ingest("a", 5, 100);
            p.ingest("a", 7, 250); // active window [200,300)
            TestRunner.checkEq(p.rangeSketch(0, 200, false).estimate("a"), 8L, "two sealed");
            TestRunner.checkEq(p.rangeSketch(0, 300, true).estimate("a"), 15L, "incl active");
        });

        runner.run();
    }
}
