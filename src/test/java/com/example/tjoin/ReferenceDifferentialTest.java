package com.example.tjoin;

import com.example.tjoin.join.IntervalJoinOperator;
import com.example.tjoin.model.BufferOverflowPolicy;
import com.example.tjoin.model.JoinConfig;
import com.example.tjoin.model.JoinResult;
import com.example.tjoin.model.SideName;
import com.example.tjoin.model.StreamEvent;
import com.example.tjoin.ref.ReferenceIntervalJoin;
import com.example.tjoin.time.ManualClock;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Random;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Differential testing against the brute-force exact reference
 * implementation. Events are generated per side in non-decreasing event
 * time (so nothing is late and nothing matchable is cleaned), then the two
 * sides are interleaved randomly. The streaming operator's emitted-pair
 * multiset must equal the reference output for every seed, key mix and
 * interval shape.
 */
class ReferenceDifferentialTest {

    private record Case(List<StreamEvent> left, List<StreamEvent> right,
                        long lower, long upper) {
    }

    @Test
    @DisplayName("200 randomized cases: streaming output == exact nested-loop reference")
    void randomizedDifferential() {
        int failures = 0;
        for (int seed = 1; seed <= 200; seed++) {
            Random rnd = new Random(seed * 7919L);
            Case c = generate(rnd);
            if (!checkOne(seed, c)) {
                failures++;
            }
        }
        assertEquals(0, failures, "differential mismatches (see stderr)");
    }

    @Test
    @DisplayName("same-timestamp duplicate-value fan-out agrees with reference")
    void denseSameTimestamp() {
        Random rnd = new Random(123);
        List<StreamEvent> left = new ArrayList<>();
        List<StreamEvent> right = new ArrayList<>();
        for (int i = 0; i < 8; i++) {
            left.add(new StreamEvent("L" + i, "k", 5000, "v"));
            right.add(new StreamEvent("R" + i, "k", 5000, "v"));
        }
        Case c = new Case(left, right, 0, 0);
        assertTrue(checkOne(999, c));
    }

    @Test
    @DisplayName("fully negative interval [-3000,-1000] agrees with reference")
    void negativeBounds() {
        Random rnd = new Random(7);
        List<StreamEvent> left = new ArrayList<>();
        List<StreamEvent> right = new ArrayList<>();
        for (int i = 0; i < 30; i++) {
            String key = "k" + rnd.nextInt(4);
            long t = 10_000 + rnd.nextInt(8_000);
            (i % 2 == 0 ? left : right)
                    .add(new StreamEvent("E" + i, key, t, null));
        }
        sortByIdOrder(left);
        sortByIdOrder(right);
        assertTrue(checkOne(42, new Case(left, right, -3000, -1000)));
    }

    // ------------------------------------------------------------------

    private boolean checkOne(int seed, Case c) {
        JoinConfig config = JoinConfig.symmetric(c.lower(), c.upper(),
                0, 0, 0, BufferOverflowPolicy.REJECT);
        ManualClock clock = new ManualClock(0L);
        IntervalJoinOperator op = new IntervalJoinOperator(config, clock, clock);

        // Merge the two per-side-ordered lists with a random interleaving.
        List<StreamEvent> ls = new ArrayList<>(c.left());
        List<StreamEvent> rs = new ArrayList<>(c.right());
        Random orderRnd = new Random(seed * 31L);
        Set<String> emittedPairs = new HashSet<>();
        int li = 0;
        int ri = 0;
        while (li < ls.size() || ri < rs.size()) {
            boolean takeLeft;
            if (li >= ls.size()) {
                takeLeft = false;
            } else if (ri >= rs.size()) {
                takeLeft = true;
            } else {
                takeLeft = orderRnd.nextBoolean();
            }
            StreamEvent ev = takeLeft ? ls.get(li++) : rs.get(ri++);
            var result = op.processEvent(takeLeft ? SideName.LEFT : SideName.RIGHT, ev);
            for (JoinResult jr : result.emitted()) {
                assertTrue(emittedPairs.add(jr.getLeftId() + "|" + jr.getRightId()),
                        "pair emitted twice: " + jr.getLeftId() + "," + jr.getRightId());
            }
        }

        Set<String> expected = new HashSet<>();
        for (var pair : ReferenceIntervalJoin.join(c.left(), c.right(), config)) {
            expected.add(pair.left().getId() + "|" + pair.right().getId());
        }

        if (!expected.equals(emittedPairs)) {
            System.err.printf("SEED %d MISMATCH: expected=%d actual=%d missing=%s extra=%s%n",
                    seed, expected.size(), emittedPairs.size(),
                    new HashSet<>(expected) {{ removeAll(emittedPairs); }},
                    new HashSet<>(emittedPairs) {{ removeAll(expected); }});
            return false;
        }
        assertEquals(expected.size(), op.metrics().pairsEmitted.get());
        return true;
    }

    private Case generate(Random rnd) {
        int nLeft = 1 + rnd.nextInt(25);
        int nRight = 1 + rnd.nextInt(25);
        int keyCount = 1 + rnd.nextInt(5);
        long span = 1 + rnd.nextInt(20_000);
        long base = rnd.nextInt(100_000);

        List<StreamEvent> left = new ArrayList<>();
        List<StreamEvent> right = new ArrayList<>();
        long idSeq = 0;
        for (int i = 0; i < nLeft; i++) {
            String key = "k" + rnd.nextInt(keyCount);
            long t = base + rnd.nextLong(span + 1);
            left.add(new StreamEvent("L" + (idSeq++), key, t, rnd.nextInt(3)));
        }
        for (int i = 0; i < nRight; i++) {
            String key = "k" + rnd.nextInt(keyCount);
            long t = base + rnd.nextLong(span + 1);
            right.add(new StreamEvent("R" + (idSeq++), key, t, rnd.nextInt(3)));
        }
        // Per-side non-decreasing event time => no late events; ties allowed.
        sortByIdOrder(left);
        sortByIdOrder(right);

        long width = rnd.nextInt(10_000);
        long shift = rnd.nextInt(6_000) - 3_000;
        long lower = shift - width;
        long upper = shift + width;
        return new Case(left, right, lower, upper);
    }

    private static void sortByIdOrder(List<StreamEvent> events) {
        events.sort(Comparator.comparingLong(StreamEvent::getTimestamp)
                .thenComparing(StreamEvent::getId));
    }
}
