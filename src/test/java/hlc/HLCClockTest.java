package hlc;

public class HLCClockTest {

    @Test("first event at a fresh physical time starts the counter at 0")
    void firstEvent() {
        HLCClock clock = new HLCClock(PhysicalClock.fixed(1000L));
        HLCTimestamp t = clock.tickLocal();
        TestRunner.assertEquals(1000L, t.l(), "l tracks physical time");
        TestRunner.assertEquals(0L, t.c(), "physical time leads -> counter resets to 0");
    }

    @Test("multiple events at the same physical instant increment only the counter")
    void sameInstantIncrementsCounter() {
        HLCClock clock = new HLCClock(PhysicalClock.fixed(1000L));
        TestRunner.assertEquals(new HLCTimestamp(1000, 0), clock.tickLocal(), "event 1 (physical leads)");
        TestRunner.assertEquals(new HLCTimestamp(1000, 1), clock.tickLocal(), "event 2 (l stuck)");
        TestRunner.assertEquals(new HLCTimestamp(1000, 2), clock.send(), "send is a local tick");
    }

    @Test("physical progress resets the counter to 0")
    void physicalProgressResetsCounter() {
        PhysicalClock.VirtualClock vc = PhysicalClock.virtual(1000L);
        HLCClock clock = new HLCClock(vc);
        clock.tickLocal();
        clock.tickLocal();
        vc.set(2000L);
        TestRunner.assertEquals(new HLCTimestamp(2000, 0), clock.tickLocal(),
                "new physical l resets c to 0");
    }

    @Test("physical clock regression never decreases a timestamp")
    void clockRegression() {
        PhysicalClock.VirtualClock vc = PhysicalClock.virtual(10_000L);
        HLCClock clock = new HLCClock(vc);
        HLCTimestamp t1 = clock.tickLocal();
        vc.set(5_000L); // wall clock leaps backwards
        HLCTimestamp t2 = clock.tickLocal();
        vc.set(1_000L); // and further back
        HLCTimestamp t3 = clock.tickLocal();
        TestRunner.assertLess(t1, t2, "regression 10k->5k still monotonic");
        TestRunner.assertLess(t2, t3, "regression 5k->1k still monotonic");
        TestRunner.assertEquals(10_000L, t3.l(), "l holds at the max seen");
        TestRunner.assertEquals(2L, t3.c(), "counter advanced through the regression");

        // Jumping forward past l resets the counter.
        vc.set(10_001L);
        HLCTimestamp t4 = clock.tickLocal();
        TestRunner.assertEquals(new HLCTimestamp(10_001, 0), t4, "recovery after regression");
    }

    @Test("receive merges a remote timestamp and stays above both parents")
    void receiveMerge() {
        PhysicalClock.VirtualClock localPhys = PhysicalClock.virtual(2_000L);
        HLCClock local = new HLCClock(localPhys);
        HLCTimestamp localFirst = local.tickLocal(); // (2000,1)

        HLCTimestamp remote = new HLCTimestamp(5_000L, 7L); // remote is ahead
        HLCTimestamp merged = local.receive(remote);
        TestRunner.assertEquals(5_000L, merged.l(), "l takes max(local, remote, physical)");
        TestRunner.assertEquals(8L, merged.c(), "c = max remote c + 1");
        TestRunner.assertLess(remote, merged, "merged strictly after message");
        TestRunner.assertLess(localFirst, merged, "merged strictly after prior local state");

        // A second receive at a frozen clock with the same l continues the counter.
        HLCTimestamp merged2 = local.receive(new HLCTimestamp(5_000L, 3L));
        TestRunner.assertEquals(new HLCTimestamp(5_000, 9), merged2, "max counter lineage wins");
    }

    @Test("receive while physical clock is behind still preserves ordering")
    void receiveWithLaggingClock() {
        PhysicalClock.VirtualClock vc = PhysicalClock.virtual(100L);
        HLCClock clock = new HLCClock(vc);
        clock.tickLocal(); // (100,1)
        HLCTimestamp out = clock.send(); // (100,2)
        vc.set(50L); // local clock fell behind before the reply arrives
        HLCTimestamp reply = new HLCTimestamp(100L, 50L);
        HLCTimestamp in = clock.receive(reply);
        TestRunner.assertEquals(new HLCTimestamp(100, 51), in, "counter merges above reply");
        TestRunner.assertLess(out, in, "receive after own send is ordered");
    }

    @Test("counter exhaustion throws explicitly and leaves state intact")
    void overflowIsExplicit() {
        long l = 9_000L;
        HLCClock clock = new HLCClock(PhysicalClock.fixed(l),
                new HLCTimestamp(l, LogicalCounterOverflowException.MAX_COUNTER));
        LogicalCounterOverflowException err = TestRunner.assertThrows(
                LogicalCounterOverflowException.class, clock::tickLocal);
        TestRunner.assertTrue(err.getMessage().contains("counter"),
                "error message explains the counter exhaustion");
        TestRunner.assertEquals(new HLCTimestamp(l, LogicalCounterOverflowException.MAX_COUNTER),
                clock.peek(), "no timestamp is produced on overflow");

        // The documented recovery: physical time advances past l.
        PhysicalClock.VirtualClock vc = PhysicalClock.virtual(l);
        HLCClock recoverable = new HLCClock(vc,
                new HLCTimestamp(l, LogicalCounterOverflowException.MAX_COUNTER));
        vc.set(l + 1);
        TestRunner.assertEquals(new HLCTimestamp(l + 1, 0), recoverable.tickLocal(),
                "advancing physical time clears the overflow condition and resets c");
    }

    @Test("restored state survives a backwards wall clock across restart")
    void restoreAcrossRestart() {
        PhysicalClock.VirtualClock before = PhysicalClock.virtual(10_000L);
        HLCClock oldProcess = new HLCClock(before);
        oldProcess.tickLocal();
        HLCTimestamp saved = oldProcess.snapshot();

        // New process boots with a clock that has moved backwards.
        HLCClock newProcess = new HLCClock(PhysicalClock.fixed(8_000L));
        newProcess.restore(saved);
        HLCTimestamp next = newProcess.tickLocal();
        TestRunner.assertLess(saved, next, "post-restore event is strictly after saved state");
        TestRunner.assertEquals(10_000L, next.l(), "l preserved from durable state");
    }

    @Test("concurrent timestamps can be equal across nodes; same-node never are")
    void concurrencySemantics() {
        HLCClock a = new HLCClock(PhysicalClock.fixed(1000L));
        HLCClock b = new HLCClock(PhysicalClock.fixed(1000L));
        HLCTimestamp ta = a.tickLocal();
        HLCTimestamp tb = b.tickLocal();
        TestRunner.assertEquals(ta, tb,
                "independent nodes at identical physical time mint equal (l,c): tie, not causality");
    }
}
