package streamagg.test;

import streamagg.engine.StreamCorrectionEngine;
import streamagg.model.IngestOp;
import streamagg.model.OpType;
import streamagg.model.OutputSnapshot;
import streamagg.time.ManualClock;
import streamagg.time.ManualScheduler;

import java.math.BigDecimal;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;

/** Deterministic clock/scheduler injection: periodic emission fires on virtual time. */
public final class TimeTest {

    public static void register(TestRunner r) {
        r.test("time: manual clock only moves when advanced", () -> {
            ManualClock c = new ManualClock(Instant.parse("2026-03-01T10:00:00Z"));
            Assert.assertEquals(Instant.parse("2026-03-01T10:00:00Z"), c.instant(), "start");
            c.advance(Duration.ofSeconds(30));
            Assert.assertEquals(Instant.parse("2026-03-01T10:00:30Z"), c.instant(), "advanced");
        });

        r.test("time: periodic output emission fires on virtual ticks", () -> {
            ManualClock c = new ManualClock();
            ManualScheduler s = new ManualScheduler(c);
            var engine = StreamCorrectionEngine.builder().clock(c).build();
            List<OutputSnapshot> snaps = new ArrayList<>();
            engine.addListener(snaps::add);
            s.scheduleAtFixedRate(Duration.ofSeconds(5), Duration.ofSeconds(5),
                    engine::emitSnapshot);

            engine.ingest(new IngestOp("a1", "a", OpType.ADD, "k", new BigDecimal("3"), null, null));
            Assert.assertEquals(0, snaps.size(), "no emission before first tick");

            int fired = s.advance(Duration.ofSeconds(5));
            Assert.assertEquals(1, fired, "one task fired");
            Assert.assertEquals(1, snaps.size(), "one snapshot");
            Assert.assertEquals(new BigDecimal("3"), snaps.get(0).sums().get("k"), "snapshot sum");
            Assert.assertEquals(1L, snaps.get(0).counts().get("k"), "snapshot count");

            s.advance(Duration.ofSeconds(15));
            Assert.assertEquals(4, snaps.size(), "3 more ticks fired");
            Assert.assertEquals(4L, snaps.get(3).sequence(), "sequence increments");
        });

        r.test("time: cancellation stops future firings", () -> {
            ManualClock c = new ManualClock();
            ManualScheduler s = new ManualScheduler(c);
            int[] count = {0};
            var task = s.scheduleAtFixedRate(Duration.ofSeconds(1), Duration.ofSeconds(1), () -> count[0]++);
            s.advance(Duration.ofSeconds(2));
            task.cancel();
            s.advance(Duration.ofSeconds(5));
            Assert.assertEquals(2, count[0], "no firings after cancel");
        });

        r.test("time: one-shot task fires once at due time", () -> {
            ManualClock c = new ManualClock();
            ManualScheduler s = new ManualScheduler(c);
            boolean[] ran = {false};
            s.schedule(Duration.ofMillis(500), () -> ran[0] = true);
            s.advance(Duration.ofMillis(499));
            Assert.assertFalse(ran[0], "not yet");
            s.advance(Duration.ofMillis(1));
            Assert.assertTrue(ran[0], "fired exactly at 500ms");
        });
    }
}
