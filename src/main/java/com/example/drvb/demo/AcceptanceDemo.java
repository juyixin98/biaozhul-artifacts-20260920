package com.example.drvb.demo;

import com.example.drvb.core.Condition;
import com.example.drvb.core.Event;
import com.example.drvb.core.Rule;
import com.example.drvb.core.RuleVersion;
import com.example.drvb.stream.IngestResult;
import com.example.drvb.stream.InMemoryResultStore;
import com.example.drvb.stream.RuleBindingEngine;
import com.example.drvb.stream.WatermarkTracker;
import com.example.drvb.core.RuleRegistry;
import com.example.drvb.time.SimClock;

import java.time.Instant;
import java.util.List;
import java.util.Map;

/**
 * Deterministic acceptance scenario for dynamic rule version binding.
 *
 * <p>Runs entirely on an injected {@link SimClock}, so every printed line is
 * exactly reproducible. Demonstrates:
 * <ol>
 *   <li>missing version before bootstrap is rejected (never latest rules);</li>
 *   <li>interleaved rule updates and out-of-order events, including boundary
 *       instants;</li>
 *   <li>late events bound to their historical version;</li>
 *   <li>events beyond the lateness horizon rejected as TOO_LATE;</li>
 *   <li>rollback by appending an immutable copy (same content checksum);</li>
 *   <li>historical-version garbage collection requiring BOTH preconditions:
 *       beyond the lateness horizon AND zero references from retained
 *       results.</li>
 * </ol>
 *
 * <p>Event times are written as ISO instants for readability; the engine uses
 * epoch milliseconds internally.
 */
public final class AcceptanceDemo {

    // Rule threshold timeline (event time):
    //   v1 (bootstrap): amount >= 100  effective -inf
    //   v2            : amount >= 200  effective 10:00:00Z
    //   v3            : amount >= 300  effective 11:00:00Z
    //   rb (rollback) : amount >= 100  effective 14:00:00Z
    //   allowed lateness 60 minutes; result retention 24 hours
    private static final String T1 = "2026-09-23T10:00:00Z";
    private static final String T2 = "2026-09-23T11:00:00Z";
    private static final String T3 = "2026-09-23T14:00:00Z";

    private final SimClock clock = new SimClock(0L);
    private final RuleBindingEngine engine = new RuleBindingEngine(
            new RuleRegistry(),
            new WatermarkTracker(0),
            new InMemoryResultStore(),
            clock,
            java.time.Duration.ofMinutes(60).toMillis(),
            java.time.Duration.ofHours(24).toMillis());

    public static void main(String[] args) {
        new AcceptanceDemo().run();
    }

    private void run() {
        section("1. Event before any rule version exists -> REJECTED "
                + "(no silent fallback to a 'latest' rule set)");
        step("processing time 09:00, ingest payment @09:30 amount=150");
        clock.set(millis("2026-09-23T09:00:00Z"));
        show(engine.ingest(payment("e-early", "2026-09-23T09:30:00Z", 150)));

        section("2. Bootstrap v1 (amount >= 100); bootstrap interval "
                + "covers all history");
        RuleVersion v1 = engine.bootstrap(version("v1", Long.MIN_VALUE,
                "initial strict threshold", 100));
        System.out.println("   bootstrapped " + v1.versionId()
                + " checksum=" + v1.checksum());

        section("3. Replay the earlier event at 09:30 -> bound to v1, MATCHES");
        show(engine.ingest(payment("e-early", "2026-09-23T09:30:00Z", 150)));

        section("4. Pre-register v2 @10:00 and v3 @11:00 while stream is at 09:30");
        RuleVersion v2 = engine.publish(version("v2", millis(T1),
                "relax threshold to 200", 200));
        RuleVersion v3 = engine.publish(version("v3", millis(T2),
                "relax threshold to 300", 300));
        System.out.println("   published " + v2.versionId() + " and " + v3.versionId()
                + " (registered in advance; binding stays purely event-time)");

        section("5. Boundary instants: 10:00:00.000 belongs to v2 "
                + "(interval is [from,next)); one ms earlier belongs to v1");
        clock.set(millis("2026-09-23T09:59:59.500Z"));
        show(engine.ingest(payment("e-at-boundary", T1, 250)));
        show(engine.ingest(payment("e-just-before",
                "2026-09-23T09:59:59.999Z", 150)));

        section("6. On-time stream at 11:30 (binds v3), then LATE events arrive "
                + "out of order and bind HISTORICAL versions");
        clock.set(millis("2026-09-23T11:30:00Z"));
        step("on-time tip @11:30 amount=350 -> v3 MATCHES; watermark = 11:30, "
                + "lateness horizon = 10:30");
        show(engine.ingest(payment("e-tip", "2026-09-23T11:30:00Z", 350)));

        step("LATE @11:00 (30 min late) amount=250 -> historical v3 interval: "
                + "no match (250 < 300)");
        show(engine.ingest(payment("e-late-1100", "2026-09-23T11:00:00Z", 250)));

        step("LATE @10:40 (50 min late, inside the window) amount=250 -> "
                + "historical v2 MATCHES (250 >= 200; under latest v3 it would NOT)");
        show(engine.ingest(payment("e-late-1040", "2026-09-23T10:40:00Z", 250)));

        section("7. Latency boundary at the 60-minute horizon "
                + "(at watermark 11:30 the horizon is exactly 10:30)");
        step("@10:30 equals the horizon: still ACCEPTED on historical v2");
        show(engine.ingest(payment("e-at-horizon", "2026-09-23T10:30:00Z", 250)));
        step("@10:29 is one minute beyond the horizon -> REJECTED TOO_LATE "
                + "(v2 is retained, but the system no longer waits this long)");
        show(engine.ingest(payment("e-beyond-horizon",
                "2026-09-23T10:29:00Z", 999)));
        step("@09:45 is far beyond the horizon -> REJECTED TOO_LATE");
        show(engine.ingest(payment("e-ancient", "2026-09-23T09:45:00Z", 150)));

        section("8. Rollback: restore v1's content effective 14:00 "
                + "(new immutable version id, identical checksum)");
        clock.set(millis("2026-09-23T14:05:00Z"));
        RuleVersion rb = engine.rollback("v1", "rb-v1-restored", millis(T3),
                "incident rollback to strict threshold");
        System.out.println("   rollback " + rb.versionId() + " checksum=" + rb.checksum()
                + " (equals v1: " + rb.checksum().equals(v1.checksum()) + ")");
        step("first a LATE @11:20 amount=250 event (processed at 14:05 but still "
                + "inside the lateness window) binds the immutable v3 interval "
                + "-> no match (250 < 300)");
        show(engine.ingest(payment("e-late-1120", "2026-09-23T11:20:00Z", 250)));
        step("then an on-time event @14:05 amount=150 matches under the restored "
                + "threshold 100, not v3 threshold 300");
        show(engine.ingest(payment("e-after-rb", "2026-09-23T14:05:00Z", 150)));

        section("9. Historical-version garbage collection: BOTH preconditions");
        step("maintenance #1 at 14:05 (horizon 13:05): no version's successor "
                + "starts at/before the horizon yet, and all results are within "
                + "the 24h retention -> purge=0, reclaim=0");
        RuleBindingEngine.GcReport gc1 = engine.runMaintenance();
        System.out.println("   purged=" + gc1.resultsPurged()
                + " reclaimed=" + gc1.versionsReclaimed()
                + " retained=" + ids(gc1.retainedVersions()));

        step("the stream advances: ingest an on-time @15:00 tip -> watermark 15:00, "
                + "horizon 14:00; v1/v2/v3 intervals now end at/before the horizon");
        clock.set(millis("2026-09-23T15:00:00Z"));
        show(engine.ingest(payment("e-tip2", "2026-09-23T15:00:00Z", 150)));
        RuleBindingEngine.GcPreview preview = engine.gcPreview();
        System.out.println("   GC preview: horizon=" + iso(preview.horizon())
                + " referenced=" + preview.referencedVersionIds()
                + " eligible=" + preview.eligibleForReclaim().stream()
                        .map(RuleVersion::versionId).toList());

        step("maintenance #2 at 15:00: time precondition is met, but retained "
                + "results still REFERENCE v1/v2/v3 -> nothing is collected");
        RuleBindingEngine.GcReport gc2 = engine.runMaintenance();
        System.out.println("   purged=" + gc2.resultsPurged()
                + " reclaimed=" + gc2.versionsReclaimed()
                + " retained=" + ids(gc2.retainedVersions()));

        step("maintenance #3 on the NEXT day (processing 15:30 next day): the "
                + "24h retention window has passed, so results purge first; in "
                + "the SAME maintenance call every unreferenced historical "
                + "version is collected, and the newest version always remains");
        clock.set(millis("2026-09-24T15:30:00Z"));
        RuleBindingEngine.GcReport gc3 = engine.runMaintenance();
        System.out.println("   purged=" + gc3.resultsPurged()
                + " reclaimed=" + gc3.versionsReclaimed()
                + " removed=" + ids(gc3.removed()));
        System.out.println("   retained=" + ids(gc3.retainedVersions()));

        step("an event in a reclaimed interval -> VERSION_RECLAIMED (never "
                + "silently re-bound to the newest rules)");
        show(engine.ingest(payment("e-after-gc", "2026-09-23T09:10:00Z", 999)));

        section("10. Final counters");
        System.out.println("   " + engine.stats());
    }

    // ------------------------------------------------------------- helpers

    private static RuleVersion version(String id, long from, String desc,
                                       int threshold) {
        Rule rule = new Rule("big-amount", "amount-above-threshold", "payment",
                new Condition.Compare("gte", "amount", threshold), "BLOCK", true);
        return new RuleVersion(id, from, 0L, desc, List.of(rule));
    }

    private static Event payment(String id, String eventTimeIso, int amount) {
        return new Event(id, "payment", millis(eventTimeIso),
                Map.of("amount", amount, "currency", "USD"));
    }

    private static long millis(String iso) {
        return Instant.parse(iso).toEpochMilli();
    }

    private static String iso(long epochMillis) {
        return Instant.ofEpochMilli(epochMillis).toString();
    }

    private static List<String> ids(List<RuleVersion> vs) {
        return vs.stream().map(RuleVersion::versionId).toList();
    }

    private static void section(String title) {
        System.out.println();
        System.out.println("── " + title);
    }

    private static void step(String text) {
        System.out.println("   - " + text);
    }

    private static void show(IngestResult r) {
        String t = iso(r.event().eventTime());
        if (r.accepted()) {
            System.out.printf("     -> ACCEPTED %-16s t=%s v=%-17s late=%-5s matches=%s wm=%s%n",
                    r.event().id(), t, r.version().versionId(), r.late(),
                    r.matchedRuleIds(), iso(r.watermark()));
        } else {
            System.out.printf("     -> REJECTED %-16s t=%s reason=%-17s wm=%s%n",
                    r.event().id(), t, r.rejectionReason(), iso(r.watermark()));
        }
    }
}
