package sessionwindow;

import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

/**
 * Unit tests for {@link SessionAggregator} — including the acceptance scenario:
 * events at t=0, t=20, then the late t=10 with gap=10 bridging the two
 * sessions, verifying retraction does not double-count and recovery produces
 * stable session ids and aggregation.
 */
final class AggregatorTest {

    private AggregatorTest() {
    }

    static TestRunner build() {
        return new TestRunner("SessionAggregator unit tests")
                .test("pure splitter: one session when adjacent gaps <= gap", () -> {
                    SessionAggregator agg = new SessionAggregator(10, 10);
                    List<List<SessionAggregator.Event>> groups =
                            SessionAggregator.splitIntoSessions(List.of(
                                    new SessionAggregator.Event("a", "k", 0, 1),
                                    new SessionAggregator.Event("b", "k", 10, 2),
                                    new SessionAggregator.Event("c", "k", 20, 3)), 10);
                    TestRunner.eq(groups.size(), 1, "all three should be one session (boundary <=)");
                })
                .test("pure splitter: splits when adjacent gap > gap", () -> {
                    List<List<SessionAggregator.Event>> groups =
                            SessionAggregator.splitIntoSessions(List.of(
                                    new SessionAggregator.Event("a", "k", 0, 1),
                                    new SessionAggregator.Event("b", "k", 20, 2)), 10);
                    TestRunner.eq(groups.size(), 2, "gap of 20 > 10 must split");
                })
                .test("acceptance: 0 then 20 emits two sessions", () -> {
                    SessionAggregator agg = new SessionAggregator(10, 10);
                    SessionAggregator.IngestResult r1 = agg.ingest("e0", "k", 0);
                    SessionAggregator.IngestResult r2 = agg.ingest("e20", "k", 20);
                    TestRunner.isTrue(r1.accepted() && r2.accepted(), "first two events accepted");
                    List<SessionAggregator.Session> sessions = agg.getSessions();
                    TestRunner.eq(sessions.size(), 2, "two separate sessions before bridge");
                    TestRunner.eq(sessions.get(0).sessionId, "k@0", "first session id");
                    TestRunner.eq(sessions.get(1).sessionId, "k@20", "second session id");
                })
                .test("acceptance: late t=10 bridges, retracts k@20, extends k@0", () -> {
                    SessionAggregator agg = new SessionAggregator(10, 10);
                    agg.ingest("e0", "k", 0);
                    agg.ingest("e20", "k", 20);
                    SessionAggregator.IngestResult bridge = agg.ingest("e10", "k", 10);

                    List<SessionAggregator.Change> changes = bridge.changes();
                    List<String> types = changes.stream()
                            .map(SessionAggregator.Change::type).collect(Collectors.toList());
                    // k@20 disappears entirely (RETRACT), k@0 grows (RETRACT+UPSERT).
                    TestRunner.eq(types, List.of("RETRACT", "RETRACT", "UPSERT"),
                            "bridge emits retract of k@20, retract+upsert of k@0");

                    SessionAggregator.Change retract20 = changes.get(0);
                    TestRunner.eq(retract20.sessionId(), "k@20", "retracted session is k@20");
                    TestRunner.eq(retract20.version(), 1L, "retracted version is 1");

                    SessionAggregator.Change upsert0 = changes.get(2);
                    TestRunner.eq(upsert0.sessionId(), "k@0", "surviving session is k@0");
                    TestRunner.eq(upsert0.version(), 2L, "k@0 is now version 2");
                    TestRunner.eq(upsert0.endTs(), 20L, "session now spans to t=20");
                    TestRunner.eq(upsert0.eventIds(), List.of("e0", "e10", "e20"),
                            "session contains all three events in time order");
                })
                .test("acceptance: final view has exactly one session, no double count", () -> {
                    SessionAggregator agg = new SessionAggregator(10, 10);
                    agg.ingest("e0", "k", 0);
                    agg.ingest("e20", "k", 20);
                    agg.ingest("e10", "k", 10);

                    List<SessionAggregator.Session> sessions = agg.getSessions();
                    TestRunner.eq(sessions.size(), 1, "bridged into exactly one session");
                    SessionAggregator.Session only = sessions.get(0);
                    TestRunner.eq(only.sessionId, "k@0", "stable session id after bridge");
                    TestRunner.eq(only.startTs, 0L, "start");
                    TestRunner.eq(only.endTs, 20L, "end");
                    TestRunner.eq(only.eventIds.size(), 3, "three distinct events");
                    TestRunner.eq(agg.getMaterializedEventRows(), 3L,
                            "materialized rows equal distinct events (no double count)");
                    TestRunner.isTrue(agg.accountingMap().get("noDoubleCount").equals(Boolean.TRUE),
                            "noDoubleCount invariant holds");
                })
                .test("acceptance: changelog net effect counts each event once", () -> {
                    SessionAggregator agg = new SessionAggregator(10, 10);
                    agg.ingest("e0", "k", 0);
                    agg.ingest("e20", "k", 20);
                    agg.ingest("e10", "k", 10);

                    // If a downstream applies UPSERTs as inserts and RETRACTs as deletes,
                    // the final row count must match.
                    long upsertRows = agg.getChangelog().stream()
                            .filter(c -> c.type().equals("UPSERT"))
                            .mapToLong(c -> c.eventIds().size())
                            .sum();
                    long retractRows = agg.getChangelog().stream()
                            .filter(c -> c.type().equals("RETRACT"))
                            .mapToLong(c -> c.eventIds().size())
                            .sum();
                    TestRunner.eq(upsertRows - retractRows, 3L,
                            "upsert rows minus retract rows nets to 3 distinct events");
                })
                .test("recovery: replay yields identical sessions, ids and versions", () -> {
                    SessionAggregator original = new SessionAggregator(10, 10);
                    original.ingest("e0", "k", 0);
                    original.ingest("e20", "k", 20);
                    original.ingest("e10", "k", 10);

                    List<SessionAggregator.Event> replayEvents = original.acceptedEventsInArrivalOrder();
                    TestRunner.eq(replayEvents.stream().map(SessionAggregator.Event::eventId).toList(),
                            List.of("e0", "e20", "e10"), "events captured in arrival order");

                    SessionAggregator recovered =
                            SessionAggregator.replay(10, 10, replayEvents);

                    List<SessionAggregator.Session> a = original.getSessions();
                    List<SessionAggregator.Session> b = recovered.getSessions();
                    TestRunner.eq(a.size(), b.size(), "same session count after recovery");
                    for (int i = 0; i < a.size(); i++) {
                        TestRunner.eq(b.get(i).sessionId, a.get(i).sessionId, "stable session id");
                        TestRunner.eq(b.get(i).version, a.get(i).version, "stable version");
                        TestRunner.eq(b.get(i).startTs, a.get(i).startTs, "stable start");
                        TestRunner.eq(b.get(i).endTs, a.get(i).endTs, "stable end");
                        TestRunner.eq(b.get(i).eventIds, a.get(i).eventIds, "stable events");
                    }
                    TestRunner.eq(recovered.getMaterializedEventRows(), 3L,
                            "recovery rows stable");
                })
                .test("watermark: event exactly at watermark is kept (<= semantics)", () -> {
                    // lateness 10: after t=100, watermark = 90. t=90 is retained.
                    SessionAggregator agg = new SessionAggregator(10, 10);
                    agg.ingest("e100", "k", 100);
                    SessionAggregator.IngestResult at = agg.ingest("e90", "k", 90);
                    TestRunner.isTrue(at.accepted(), "t=90 == watermark 90 must be accepted");
                    TestRunner.eq(agg.getRejectedCount(), 0L, "no rejects at boundary");
                })
                .test("watermark: event below watermark is rejected and not counted", () -> {
                    SessionAggregator agg = new SessionAggregator(10, 10);
                    agg.ingest("e100", "k", 100); // watermark -> 90
                    SessionAggregator.IngestResult late = agg.ingest("e89", "k", 89);
                    TestRunner.isTrue(!late.accepted(), "t=89 < watermark 90 rejected");
                    TestRunner.isTrue(late.changes().isEmpty(), "rejection emits no changes");
                    TestRunner.eq(agg.getRejectedCount(), 1L, "one rejected event");
                    TestRunner.eq(agg.getMaterializedEventRows(), 1L,
                            "rejected event never enters aggregation");
                    TestRunner.eq(agg.getSessions().size(), 1, "still one session");
                })
                .test("watermark: zero lateness rejects anything earlier than max", () -> {
                    SessionAggregator agg = new SessionAggregator(10, 0);
                    agg.ingest("e10", "k", 10);
                    SessionAggregator.IngestResult r = agg.ingest("e9", "k", 9);
                    TestRunner.isTrue(!r.accepted(), "with lateness 0, t=9 < wm 10 rejected");
                })
                .test("gap boundary: difference exactly gap stays merged", () -> {
                    SessionAggregator agg = new SessionAggregator(5, 100);
                    agg.ingest("a", "k", 0);
                    agg.ingest("b", "k", 5);
                    agg.ingest("c", "k", 10);
                    TestRunner.eq(agg.getSessions().size(), 1, "0,5,10 with gap=5 is one session");
                })
                .test("gap boundary: difference gap+1 splits", () -> {
                    SessionAggregator agg = new SessionAggregator(5, 100);
                    agg.ingest("a", "k", 0);
                    agg.ingest("b", "k", 6);
                    TestRunner.eq(agg.getSessions().size(), 2, "0 and 6 with gap=5 split");
                })
                .test("multiple keys aggregate independently", () -> {
                    SessionAggregator agg = new SessionAggregator(10, 100);
                    agg.ingest("a", "x", 0);
                    agg.ingest("b", "x", 10);
                    agg.ingest("c", "y", 0);
                    agg.ingest("d", "y", 50);
                    List<SessionAggregator.Session> ss = agg.getSessions();
                    TestRunner.eq(ss.size(), 3, "x merged (1) + y split (2) = 3");
                    TestRunner.eq(ss.get(0).key, "x", "ordered by key then start");
                })
                .test("duplicate eventId+timestamp is idempotent", () -> {
                    SessionAggregator agg = new SessionAggregator(10, 100);
                    agg.ingest("dup", "k", 0);
                    SessionAggregator.IngestResult again = agg.ingest("dup", "k", 0);
                    TestRunner.isTrue(again.accepted() && again.duplicate(), "marked duplicate");
                    TestRunner.isTrue(again.changes().isEmpty(), "duplicate emits no changes");
                    TestRunner.eq(agg.getAcceptedCount(), 1L, "accepted count not inflated");
                    TestRunner.eq(agg.getMaterializedEventRows(), 1L, "rows not inflated");
                })
                .test("same eventId with changed timestamp conflicts", () -> {
                    SessionAggregator agg = new SessionAggregator(10, 100);
                    agg.ingest("dup", "k", 0);
                    boolean threw = false;
                    try {
                        agg.ingest("dup", "k", 5);
                    } catch (SessionAggregator.DuplicateIdException e) {
                        threw = true;
                    }
                    TestRunner.isTrue(threw, "reusing eventId with new ts throws DuplicateIdException");
                })
                .test("unchanged session emits no messages on unrelated ingest", () -> {
                    SessionAggregator agg = new SessionAggregator(10, 100);
                    agg.ingest("a", "x", 0);
                    SessionAggregator.IngestResult otherKey = agg.ingest("b", "y", 100);
                    TestRunner.eq(otherKey.changes().size(), 1, "only the y session emits");
                    TestRunner.eq(otherKey.changes().get(0).key(), "y", "change belongs to y");
                })
                .test("three-way bridge: late middle event joins three sessions", () -> {
                    // gap=5: 0, 6, 12 are three sessions; inserting 5 bridges 0..6,
                    // then 7 (still late) bridges into 12 too -> one session.
                    SessionAggregator agg = new SessionAggregator(5, 100);
                    agg.ingest("a", "k", 0);
                    agg.ingest("b", "k", 6);
                    agg.ingest("c", "k", 12);
                    TestRunner.eq(agg.getSessions().size(), 3, "three sessions initially");
                    agg.ingest("late1", "k", 5);
                    TestRunner.eq(agg.getSessions().size(), 2, "5 bridges first two");
                    agg.ingest("late2", "k", 7);
                    List<SessionAggregator.Session> ss = agg.getSessions();
                    TestRunner.eq(ss.size(), 1, "7 bridges all three");
                    TestRunner.eq(ss.get(0).eventIds, List.of("a", "late1", "b", "late2", "c"),
                            "all events merged in order");
                    TestRunner.eq(agg.getMaterializedEventRows(), 5L, "no double count after 3-way merge");
                })
                .test("sessions sorted by (key, startTs)", () -> {
                    SessionAggregator agg = new SessionAggregator(1, 1000);
                    agg.ingest("a", "k", 30);
                    agg.ingest("b", "k", 0);
                    List<SessionAggregator.Session> ss = agg.getSessions();
                    TestRunner.eq(ss.get(0).startTs, 0L, "earlier start first");
                    TestRunner.eq(ss.get(1).startTs, 30L, "later start second");
                });
    }
}
