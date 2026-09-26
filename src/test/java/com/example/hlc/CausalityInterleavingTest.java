package com.example.hlc;

import com.example.hlc.sim.HlcSimulation;
import com.example.hlc.sim.HlcSimulation.SimEvent;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Acceptance tests: with virtual clocks and scripted message interleavings,
 * verify that causality implies timestamp order (a -&gt; b  =&gt;  ts(a) &lt; ts(b)),
 * and demonstrate explicitly that the converse does NOT hold.
 */
class CausalityInterleavingTest {

    /**
     * Fixed interleaving across three nodes with skewed virtual clocks,
     * including a physical clock rollback mid-scenario.
     */
    private HlcSimulation fixedScenario() {
        HlcSimulation sim = new HlcSimulation();
        sim.addNode("A", 1_000);
        sim.addNode("B", 5_000); // wall clock skewed ahead
        sim.addNode("C", 2_000);

        sim.local("A", "a1");
        sim.send("A", "B", "a2-send", "b1-recv");
        sim.local("B", "b2");
        sim.local("C", "c1");
        sim.send("C", "A", "c2-send", "a3-recv");
        sim.send("B", "C", "b3-send", "c3-recv");

        // Physical clock rollback on A must not disturb causality tracking.
        sim.clockOf("A").rewind(30_000);
        sim.local("A", "a4");
        sim.send("A", "B", "a5-send", "b4-recv");
        sim.local("B", "b5");
        return sim;
    }

    @Test
    void causalityImpliesTimestampOrder() {
        HlcSimulation sim = fixedScenario();
        List<SimEvent> events = sim.events();

        int causalPairs = 0;
        for (SimEvent x : events) {
            for (SimEvent y : events) {
                if (x.id().equals(y.id())) {
                    continue;
                }
                if (sim.happensBefore(x.id(), y.id())) {
                    causalPairs++;
                    assertTrue(x.timestamp().compareTo(y.timestamp()) < 0,
                            "causality violated: " + x.id() + " -> " + y.id()
                                    + " but " + x.timestamp() + " !< " + y.timestamp());
                }
            }
        }
        assertTrue(causalPairs > 0, "scenario must contain causal pairs");
        System.out.println("verified " + causalPairs + " causal pairs over " + events.size() + " events");
    }

    @Test
    void timestampOrderDoesNotImplyCausality() {
        HlcSimulation sim = fixedScenario();

        // a1 (A, wall 1000) and c1 (C, wall 2000) are concurrent: no messages
        // passed between A and C before either event. Yet their timestamps are
        // strictly ordered because the order on HLC timestamps is total.
        SimEvent a1 = sim.event("a1");
        SimEvent c1 = sim.event("c1");

        assertFalse(sim.happensBefore("a1", "c1"), "a1 and c1 must be concurrent");
        assertFalse(sim.happensBefore("c1", "a1"), "a1 and c1 must be concurrent");
        assertTrue(a1.timestamp().compareTo(c1.timestamp()) < 0,
                "timestamps are totally ordered even for concurrent events: "
                        + a1.timestamp() + " < " + c1.timestamp()
                        + " — but neither event caused the other");
    }

    /**
     * Randomized interleavings with fixed seeds (deterministic, reproducible):
     * the causality =&gt; timestamp-order invariant must hold universally.
     */
    @Test
    void randomizedInterleavingsPreserveCausalityInvariant() {
        long[] fixedSeeds = {42L, 1337L, 20260922L, 7L, 99L};
        int totalCausalPairs = 0;

        for (long seed : fixedSeeds) {
            Random rng = new Random(seed);
            HlcSimulation sim = new HlcSimulation();
            String[] nodes = {"A", "B", "C", "D"};
            for (int i = 0; i < nodes.length; i++) {
                sim.addNode(nodes[i], 1_000 + rng.nextInt(10_000));
            }

            int eventCounter = 0;
            for (int step = 0; step < 60; step++) {
                String node = nodes[rng.nextInt(nodes.length)];
                int action = rng.nextInt(10);
                if (action < 4) {
                    sim.local(node, "e" + eventCounter++);
                } else if (action < 8) {
                    String target = nodes[rng.nextInt(nodes.length)];
                    if (!target.equals(node)) {
                        sim.send(node, target, "e" + eventCounter++, "e" + eventCounter++);
                    }
                } else if (action < 9) {
                    sim.clockOf(node).advance(rng.nextInt(500));
                } else {
                    // Occasional physical clock rollback.
                    sim.clockOf(node).rewind(rng.nextInt(2_000));
                }
            }

            List<SimEvent> events = sim.events();
            List<String> violations = new ArrayList<>();
            for (SimEvent x : events) {
                for (SimEvent y : events) {
                    if (!x.id().equals(y.id()) && sim.happensBefore(x.id(), y.id())) {
                        totalCausalPairs++;
                        if (x.timestamp().compareTo(y.timestamp()) >= 0) {
                            violations.add(x.id() + "->" + y.id()
                                    + " ts " + x.timestamp() + " !< " + y.timestamp());
                        }
                    }
                }
            }
            assertTrue(violations.isEmpty(),
                    "seed " + seed + " causality violations: " + violations);
        }
        assertTrue(totalCausalPairs > 0);
        System.out.println("randomized scenarios verified " + totalCausalPairs + " causal pairs, 0 violations");
    }
}
