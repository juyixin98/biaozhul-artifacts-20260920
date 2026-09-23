package io.example.orderedcommit;

import java.util.List;
import java.util.Map;

/**
 * Core state-machine tests against {@link OrderedEventService} directly (no
 * HTTP). These cover the acceptance scenarios: head-of-line buffering, buffer
 * cap, retry-exhaustion order, cancellation, in-flight cap and partition
 * independence.
 */
final class OrderingCoreTest implements AutoCloseable {

    private final ServiceFixture fixture = ServiceFixture.start(8, 100);

    @Test
    void laterEventFinishesFirstButCommittedInSeqOrder() throws Exception {
        String p = "p-order";
        fixture.service.createPartition(p, 100L);

        var e0 = fixture.submit(p, 400, false, null, 1, null); // slow head
        var e1 = fixture.submit(p, 50, false, null, 1, null); // fast tail
        var e2 = fixture.submit(p, 80, false, null, 1, null);

        // The tails finish first...
        Asserts.waitFor(2_000, "e1/e2 SUCCEEDED", () -> {
            Asserts.assertEquals("SUCCEEDED",
                    ServiceFixture.status(fixture.service.getEvent(p, ServiceFixture.id(e1))),
                    "e1 should finish before slow head");
            Asserts.assertEquals("SUCCEEDED",
                    ServiceFixture.status(fixture.service.getEvent(p, ServiceFixture.id(e2))),
                    "e2 should finish before slow head");
        });
        // ...but nothing is committed past the head yet.
        Asserts.assertEquals(0, fixture.committed(p).size(),
                "no output while head is still running");
        Asserts.assertEquals("RUNNING",
                ServiceFixture.status(fixture.service.getEvent(p, ServiceFixture.id(e0))),
                "head still running");

        Asserts.waitFor(3_000, "all committed", () -> {
            Asserts.assertEquals(3, fixture.committed(p).size(), "all three committed");
        });

        List<Map<String, Object>> out = fixture.committed(p);
        Asserts.assertEquals(0L, ServiceFixture.committedSeq(out.get(0)), "out[0] seq");
        Asserts.assertEquals(1L, ServiceFixture.committedSeq(out.get(1)), "out[1] seq");
        Asserts.assertEquals(2L, ServiceFixture.committedSeq(out.get(2)), "out[2] seq");
        Asserts.assertEquals("SUCCESS", ServiceFixture.outcome(out.get(0)), "out[0] outcome");
        Asserts.assertEquals("SUCCESS", ServiceFixture.outcome(out.get(2)), "out[2] outcome");
        Asserts.assertEquals("COMMITTED",
                ServiceFixture.status(fixture.service.getEvent(p, ServiceFixture.id(e1))),
                "tail becomes COMMITTED");
    }

    @Test
    void timeoutOnHeadReleasesOrderWithFailurePlaceholder() throws Exception {
        String p = "p-timeout";
        fixture.service.createPartition(p, 100L);

        var e0 = fixture.submit(p, 1_000, false, 100L, 1, null); // times out
        var e1 = fixture.submit(p, 30, false, 10_000L, 1, null);

        Asserts.waitFor(3_000, "timeout head commits FAILURE", () -> {
            List<Map<String, Object>> out = fixture.committed(p);
            Asserts.assertEquals(2, out.size(), "both released after head timeout");
            Asserts.assertEquals(0L, ServiceFixture.committedSeq(out.get(0)), "head seq");
            Asserts.assertEquals("FAILURE", ServiceFixture.outcome(out.get(0)),
                    "timeout becomes failure placeholder");
            Asserts.assertEquals("SUCCESS", ServiceFixture.outcome(out.get(1)),
                    "tail still succeeds");
        });

        // FAILED is terminal: the failure placeholder is committed but the
        // event keeps FAILED (only successes move SUCCEEDED -> COMMITTED).
        Asserts.assertEquals("FAILED", ServiceFixture.status(
                fixture.service.getEvent(p, ServiceFixture.id(e0))),
                "timed-out event is FAILED after placeholder commit");
    }

    @Test
    void partitionsAreIndependent() throws Exception {
        fixture.service.createPartition("a", 100L);
        fixture.service.createPartition("b", 100L);

        var a0 = fixture.submit("a", 500, false, null, 1, null);
        var b0 = fixture.submit("b", 30, false, null, 1, null);

        Asserts.waitFor(2_000, "partition b commits while a blocked", () -> {
            Asserts.assertEquals(1, fixture.committed("b").size(), "b committed");
        });
        Asserts.assertEquals(0, fixture.committed("a").size(),
                "a head still buffering");
        Asserts.assertEquals(
                ServiceFixture.id(b0),
                fixture.committed("b").get(0).get("eventId"),
                "b output identity");
        Asserts.assertTrue(
                fixture.service.getEvent("a", ServiceFixture.id(a0)).containsKey("seq"),
                "a event still queryable");

        Asserts.waitFor(2_000, "partition a eventually commits", () -> {
            Asserts.assertEquals(1, fixture.committed("a").size(), "a committed");
        });
    }

    @Test
    void bufferCapRejectsExtraEventsWith429() {
        String p = "p-buffer";
        fixture.service.createPartition(p, 2L);
        fixture.submit(p, 2_000, false, null, 1, null);
        fixture.submit(p, 2_000, false, null, 1, null);

        try {
            fixture.submit(p, 10, false, null, 1, null);
            throw new AssertionError("expected 429 from full partition buffer");
        } catch (EventException e) {
            Asserts.assertEquals(429, e.httpStatus(), "buffer overflow status");
        }
    }

    @Test
    void inflightCapBoundsRunningEvents() throws Exception {
        try (ServiceFixture tiny = ServiceFixture.start(2, 100)) {
            tiny.service.createPartition("p", 100L);
            tiny.submit("p", 1_000, false, null, 1, null);
            tiny.submit("p", 1_000, false, null, 1, null);
            tiny.submit("p", 1_000, false, null, 1, null);
            tiny.submit("p", 1_000, false, null, 1, null);

            Thread.sleep(150);
            Map<String, Object> stats = tiny.service.partitionStats("p");
            Asserts.assertEquals(2L, ((Number) stats.get("inFlight")).longValue(),
                    "at most inflightCap running");
            Asserts.assertEquals(2L, ((Number) stats.get("buffered")).longValue(),
                    "rest stay buffered");
            Asserts.assertEquals(4L, ((Number) stats.get("outstanding")).longValue(),
                    "all four occupy slots");
        }
    }

    @Test
    void failedAttemptsAreRetriedThenPlaceholderCommittedInOrder() throws Exception {
        String p = "p-retry";
        fixture.service.createPartition(p, 100L);

        var fail = fixture.submit(p, 20, true, 10_000L, 3, 30L);
        var ok = fixture.submit(p, 20, false, 10_000L, 1, null);

        Asserts.waitFor(3_000, "failure + success committed", () -> {
            Asserts.assertEquals(2, fixture.committed(p).size(), "two outputs");
        });
        List<Map<String, Object>> out = fixture.committed(p);
        Asserts.assertEquals(0L, ServiceFixture.committedSeq(out.get(0)), "failure seq");
        Asserts.assertEquals("FAILURE", ServiceFixture.outcome(out.get(0)),
                "failure placeholder keeps its slot");
        Asserts.assertEquals(3L, ((Number) out.get(0).get("attempts")).longValue(),
                "three attempts recorded");
        Asserts.assertEquals("SUCCESS", ServiceFixture.outcome(out.get(1)),
                "later success committed after placeholder");
        Asserts.assertEquals(3L, ServiceFixture.attempts(
                        fixture.service.getEvent(p, ServiceFixture.id(fail))),
                "failed event attempts");
        Asserts.assertEquals(1L, ServiceFixture.attempts(
                        fixture.service.getEvent(p, ServiceFixture.id(ok))),
                "succeeded event attempts");
        Asserts.assertTrue(String.valueOf(out.get(0).get("error")).contains("injected"),
                "placeholder carries error text: " + out.get(0).get("error"));
    }

    @Test
    void timedOutAttemptIsRetriedAndSucceedsOnSecondAttempt() throws Exception {
        String p = "p-retry-timeout";

        // Custom processor: first attempt sleeps long enough to time out,
        // later attempts return immediately.
        OrderedEventService.Config config =
                new OrderedEventService.Config(4, 100, 50, 10_000, 1, 10);
        OrderedEventService custom = new OrderedEventService(config, new EventProcessor() {
            @Override
            public Object process(Event event) {
                if (event.attempts == 1) {
                    try {
                        Thread.sleep(500);
                    } catch (InterruptedException e) {
                        throw new RuntimeException("timeout interrupt", e);
                    }
                }
                return java.util.Map.of("attempt", event.attempts);
            }
        });
        try {
            custom.createPartition(p, 100L);
            Map<String, Object> view = custom.submit(p, Json.readObject(
                    "{\"timeoutMillis\":80,\"maxAttempts\":2,\"retryDelayMillis\":20}"));
            String id = ServiceFixture.id(view);
            Asserts.waitFor(3_000, "retried event commits", () -> {
                List<Map<String, Object>> out = custom.results(p, -1, 0);
                Asserts.assertEquals(1, out.size(), "one output");
                Asserts.assertEquals("SUCCESS", ServiceFixture.outcome(out.get(0)),
                        "succeeds after retry");
                Asserts.assertEquals(2L, ((Number) out.get(0).get("attempts")).longValue(),
                        "placeholder attempts is 2");
            });
            Asserts.assertEquals(2L,
                    ServiceFixture.attempts(custom.getEvent(p, id)),
                    "event used both attempts");
        } finally {
            custom.shutdown();
        }
    }

    @Test
    void cancelQueuedEventProducesNoOutputAndUnblocksTail() throws Exception {
        try (ServiceFixture tiny = ServiceFixture.start(1, 100)) {
            String p = "p-cancel-queued";
            tiny.service.createPartition(p, 100L);
            var head = tiny.submit(p, 1_000, false, null, 1, null);
            var queued = tiny.submit(p, 1_000, false, null, 1, null);
            var tail = tiny.submit(p, 30, false, null, 1, null);

            Thread.sleep(100);
            tiny.service.cancel(p, ServiceFixture.id(queued), "test: drop queued");

            // Wait for head to finish and tail to commit; queued must never appear.
            Asserts.waitFor(3_000, "head + tail committed, queued skipped", () -> {
                Asserts.assertEquals(2, tiny.committed(p).size(), "two outputs");
            });
            List<Map<String, Object>> out = tiny.committed(p);
            Asserts.assertEquals(0L, ServiceFixture.committedSeq(out.get(0)), "head seq");
            Asserts.assertEquals(2L, ServiceFixture.committedSeq(out.get(1)),
                    "tail seq — seq 1 skipped, no gap in output but identity preserved");
            Asserts.assertEquals(ServiceFixture.id(tail), out.get(1).get("eventId"),
                    "tail identity");
            Asserts.assertEquals("CANCELLED",
                    ServiceFixture.status(tiny.service.getEvent(p, ServiceFixture.id(queued))),
                    "queued event cancelled");
        }
    }

    @Test
    void cancelRunningEventInterruptsAndSkipsItsSlot() throws Exception {
        String p = "p-cancel-running";
        fixture.service.createPartition(p, 100L);
        var running = fixture.submit(p, 3_000, false, null, 1, null);
        var tail = fixture.submit(p, 30, false, null, 1, null);

        Thread.sleep(100);
        fixture.service.cancel(p, ServiceFixture.id(running), "test: abort");
        Asserts.waitFor(3_000, "tail commits after running cancel", () -> {
            List<Map<String, Object>> out = fixture.committed(p);
            Asserts.assertEquals(1, out.size(), "only tail output");
            Asserts.assertEquals(1L, ServiceFixture.committedSeq(out.get(0)), "tail seq");
        });
        Asserts.assertEquals("CANCELLED",
                ServiceFixture.status(fixture.service.getEvent(p, ServiceFixture.id(running))),
                "running event marked cancelled");
        Asserts.assertEquals(null,
                fixture.service.getEvent(p, ServiceFixture.id(running)).get("result"),
                "cancelled event carries no result");
    }

    @Test
    void cancelFinishedEventIsConflictAndIdempotentOtherwise() throws Exception {
        String p = "p-cancel-conflict";
        fixture.service.createPartition(p, 100L);
        var done = fixture.submit(p, 20, false, null, 1, null);
        Asserts.waitFor(2_000, "event committed", () -> {
            Asserts.assertEquals(1, fixture.committed(p).size(), "committed");
        });
        try {
            fixture.service.cancel(p, ServiceFixture.id(done), null);
            throw new AssertionError("expected 409 cancelling finished event");
        } catch (EventException e) {
            Asserts.assertEquals(409, e.httpStatus(), "finished cancel status");
        }
    }

    @Test
    void cancelIsIdempotent() throws Exception {
        String p = "p-cancel-idem";
        fixture.service.createPartition(p, 100L);
        var e = fixture.submit(p, 3_000, false, null, 1, null);
        Thread.sleep(50);
        var first = fixture.service.cancel(p, ServiceFixture.id(e), "first");
        var second = fixture.service.cancel(p, ServiceFixture.id(e), "second");
        Asserts.assertEquals("CANCELLED", ServiceFixture.status(first), "first cancel");
        Asserts.assertEquals("CANCELLED", ServiceFixture.status(second), "second cancel no-op");
        Asserts.assertEquals("first", second.get("cancelReason"), "reason preserved");
    }

    @Override
    public void close() {
        fixture.close();
    }
}
