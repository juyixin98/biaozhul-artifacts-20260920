package com.example.hlc;

import com.example.hlc.clock.VirtualClock;
import com.example.hlc.core.ClockDriftException;
import com.example.hlc.core.HlcTimestamp;
import com.example.hlc.core.HybridLogicalClock;
import com.example.hlc.core.LogicalOverflowException;
import com.example.hlc.core.OverflowPolicy;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class HybridLogicalClockTest {

    @Test
    void tickFollowsForwardMovingWallClock() {
        VirtualClock vc = new VirtualClock(1000);
        HybridLogicalClock hlc = HybridLogicalClock.builder(vc, "n1").build();

        HlcTimestamp t1 = hlc.tick();
        assertEquals(new HlcTimestamp(1000, 0, "n1"), t1);

        vc.advance(10);
        HlcTimestamp t2 = hlc.tick();
        assertEquals(new HlcTimestamp(1010, 0, "n1"), t2);
    }

    @Test
    void physicalClockRollbackDoesNotBreakMonotonicity() {
        VirtualClock vc = new VirtualClock(10_000);
        HybridLogicalClock hlc = HybridLogicalClock.builder(vc, "n1").build();

        List<HlcTimestamp> produced = new ArrayList<>();
        produced.add(hlc.tick());
        vc.advance(5);
        produced.add(hlc.tick());

        // Physical clock jumps 60 seconds backwards (NTP step, suspend/resume).
        vc.rewind(60_000);
        for (int i = 0; i < 100; i++) {
            produced.add(hlc.tick());
        }

        for (int i = 1; i < produced.size(); i++) {
            assertTrue(produced.get(i).compareTo(produced.get(i - 1)) > 0,
                    "timestamps must strictly increase even across rollback: "
                            + produced.get(i - 1) + " !< " + produced.get(i));
        }
        // The HLC physical component must not follow the wall clock backwards.
        HlcTimestamp last = produced.get(produced.size() - 1);
        assertTrue(last.physicalMillis() >= 10_005,
                "physical component must stay >= pre-rollback value, got " + last.physicalMillis());
    }

    @Test
    void receiveMergesRemoteTimestampAheadOfLocal() {
        VirtualClock vc = new VirtualClock(1000);
        HybridLogicalClock hlc = HybridLogicalClock.builder(vc, "local").build();
        hlc.tick(); // (1000, 0)

        HlcTimestamp remote = new HlcTimestamp(5000, 7, "remote");
        HlcTimestamp merged = hlc.receive(remote);

        assertEquals(5000, merged.physicalMillis());
        assertEquals(8, merged.logical());
        assertTrue(merged.compareTo(remote) > 0, "receive event must be stamped after the remote send");
    }

    @Test
    void receiveWithSamePhysicalBumpsMaxLogical() {
        VirtualClock vc = new VirtualClock(1000);
        HybridLogicalClock hlc = HybridLogicalClock.builder(vc, "local").build();
        hlc.tick(); // (1000, 0)
        hlc.tick(); // (1000, 1)

        HlcTimestamp merged = hlc.receive(new HlcTimestamp(1000, 5, "remote"));
        assertEquals(new HlcTimestamp(1000, 6, "local"), merged);
    }

    @Test
    void receiveOlderRemoteStillAdvancesLocal() {
        VirtualClock vc = new VirtualClock(9000);
        HybridLogicalClock hlc = HybridLogicalClock.builder(vc, "local").build();
        HlcTimestamp before = hlc.tick(); // (9000, 0)

        HlcTimestamp merged = hlc.receive(new HlcTimestamp(1000, 0, "remote"));
        assertTrue(merged.compareTo(before) > 0);
        assertEquals(9000, merged.physicalMillis());
    }

    @Test
    void logicalOverflowBumpPhysicalPolicyAdvancesPhysicalComponent() {
        VirtualClock vc = new VirtualClock(1000); // frozen wall clock
        HybridLogicalClock hlc = HybridLogicalClock.builder(vc, "n1")
                .maxLogical(3)
                .overflowPolicy(OverflowPolicy.BUMP_PHYSICAL)
                .build();

        hlc.tick(); // (1000, 0)
        hlc.tick(); // (1000, 1)
        hlc.tick(); // (1000, 2)
        hlc.tick(); // (1000, 3) — at max
        HlcTimestamp afterOverflow = hlc.tick(); // overflow: physical bumped

        assertEquals(new HlcTimestamp(1001, 0, "n1"), afterOverflow);
    }

    @Test
    void logicalOverflowThrowPolicyRaisesExplicitException() {
        VirtualClock vc = new VirtualClock(1000); // frozen wall clock
        HybridLogicalClock hlc = HybridLogicalClock.builder(vc, "n1")
                .maxLogical(2)
                .overflowPolicy(OverflowPolicy.THROW)
                .build();

        hlc.tick(); // (1000, 0)
        hlc.tick(); // (1000, 1)
        hlc.tick(); // (1000, 2) — at max
        LogicalOverflowException ex = assertThrows(LogicalOverflowException.class, hlc::tick);
        assertTrue(ex.getMessage().contains("maxLogical=2"));

        // After the wall clock advances, ticking works again.
        vc.advance(1);
        assertEquals(new HlcTimestamp(1001, 0, "n1"), hlc.tick());
    }

    @Test
    void receiveRejectsRemoteTimestampBeyondMaxDrift() {
        VirtualClock vc = new VirtualClock(1000);
        HybridLogicalClock hlc = HybridLogicalClock.builder(vc, "local")
                .maxDriftMillis(60_000)
                .build();

        HlcTimestamp tooFarAhead = new HlcTimestamp(1000 + 60_001, 0, "remote");
        assertThrows(ClockDriftException.class, () -> hlc.receive(tooFarAhead));

        // Within the drift bound: accepted.
        HlcTimestamp ok = hlc.receive(new HlcTimestamp(1000 + 60_000, 0, "remote"));
        assertEquals(1000 + 60_000, ok.physicalMillis());
    }
}
