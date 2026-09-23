package seqcep;

import seqcep.engine.Event;
import seqcep.engine.Match;
import seqcep.engine.MatchEngine;

import java.util.List;

import static seqcep.TestRunner.*;

/**
 * Matching-semantics tests, including the acceptance scenarios from the specification.
 */
public final class EngineTests {

    public static void register() {

        // ---- acceptance: "A,A,B,C" must produce 2 matches (overlapping, no consumption)
        test("A,A,B,C yields 2 overlapping matches", () -> {
            MatchEngine e = MatchEngine.inMemory("t");
            e.ingest("A", "e1", 1000);
            e.ingest("A", "e1", 2000);
            e.ingest("B", "e1", 3000);
            e.ingest("C", "e1", 4000);

            List<Match> m = e.matches("e1");
            assertEquals(2, m.size(), "match count");
            assertEquals(1, m.get(0).seqA(), "first match uses A#1");
            assertEquals(2, m.get(1).seqA(), "second match uses A#2");
            assertEquals(3, m.get(0).seqB(), "both matches share B#3");
            assertEquals(4, m.get(0).seqC(), "both matches share C#4");
            e.close();
        });

        // ---- acceptance: "A,B,timeout,C" must produce 0 matches
        test("A,B,timeout,C yields 0 matches", () -> {
            MatchEngine e = MatchEngine.inMemory("t");
            e.ingest("A", "e1", 1000);
            e.ingest("B", "e1", 2000);
            // gap of 11 seconds > 10 s window
            e.ingest("C", "e1", 13000);
            assertEquals(0, e.matchCount("e1"), "match count after timeout");

            // and a subsequent in-window chain still matches from scratch
            e.ingest("A", "e1", 20000);
            e.ingest("B", "e1", 21000);
            e.ingest("C", "e1", 22000);
            assertEquals(1, e.matchCount("e1"), "match count after fresh chain");
            e.close();
        });

        // ---- window boundary: exactly 10_000 ms counts, 10_001 does not
        test("window boundary is inclusive at 10000 ms", () -> {
            MatchEngine e = MatchEngine.inMemory("t");
            e.ingest("A", "in", 0);
            e.ingest("B", "in", 5000);
            e.ingest("C", "in", 10000); // exactly at the boundary
            assertEquals(1, e.matchCount("in"), "boundary match");

            e.ingest("A", "out", 0);
            e.ingest("B", "out", 5000);
            e.ingest("C", "out", 10001); // one ms too late
            assertEquals(0, e.matchCount("out"), "one ms past window");
            e.close();
        });

        // ---- irrelevant events are skipped and do not disturb partial state
        test("irrelevant events are skipped", () -> {
            MatchEngine e = MatchEngine.inMemory("t");
            e.ingest("A", "e1", 1000);
            e.ingest("X", "e1", 1500);   // noise
            e.ingest("HEARTBEAT", "e1", 1800); // noise
            e.ingest("B", "e1", 2000);
            e.ingest("X", "e1", 2500);   // noise
            e.ingest("C", "e1", 3000);
            assertEquals(1, e.matchCount("e1"), "noise must not break the chain");

            // noise alone never creates state that matches
            MatchEngine e2 = MatchEngine.inMemory("t2");
            e2.ingest("X", "e1", 1000);
            e2.ingest("C", "e1", 2000);
            assertEquals(0, e2.matchCount("e1"), "noise alone matches nothing");
            e.close();
            e2.close();
        });

        // ---- same timestamp: input seq decides order
        test("same timestamp ordered by input seq", () -> {
            MatchEngine e = MatchEngine.inMemory("t");
            // A and B share a timestamp; A arrived first so A(seq1) <= B(seq2) holds.
            e.ingest("A", "e1", 1000);
            e.ingest("B", "e1", 1000);
            e.ingest("C", "e1", 1000);
            assertEquals(1, e.matchCount("e1"), "equal timestamps in input order match");

            // Reverse input order at the same timestamp: B first, then A.
            // The B cannot pair with the later A (seq decides), so no match.
            MatchEngine e2 = MatchEngine.inMemory("t2");
            e2.ingest("B", "e1", 1000);
            e2.ingest("A", "e1", 1000);
            e2.ingest("C", "e1", 1000);
            assertEquals(0, e2.matchCount("e1"), "B before A at same ts must not match");
            e.close();
            e2.close();
        });

        // ---- entities are fully isolated
        test("entities are isolated", () -> {
            MatchEngine e = MatchEngine.inMemory("t");
            e.ingest("A", "e1", 1000);
            e.ingest("A", "e2", 1000);
            e.ingest("B", "e1", 2000);
            e.ingest("C", "e2", 2000); // e2 has A but no B -> no match
            e.ingest("C", "e1", 3000);
            assertEquals(1, e.matchCount("e1"), "e1 matches");
            assertEquals(0, e.matchCount("e2"), "e2 does not match");
            assertEquals(1, e.matchCount(null), "global count");
            e.close();
        });

        // ---- full combination enumeration: A,A,B,B,C -> 4 matches, nothing truncated
        test("A,A,B,B,C enumerates all 4 combinations", () -> {
            MatchEngine e = MatchEngine.inMemory("t");
            e.ingest("A", "e1", 1000);
            e.ingest("A", "e1", 1100);
            e.ingest("B", "e1", 2000);
            e.ingest("B", "e1", 2100);
            e.ingest("C", "e1", 3000);
            List<Match> m = e.matches("e1");
            assertEquals(4, m.size(), "2 A x 2 B x 1 C = 4 matches");
            e.close();
        });

        // ---- matches reuse events across overlapping windows
        test("one C completes several pending pairs", () -> {
            MatchEngine e = MatchEngine.inMemory("t");
            e.ingest("A", "e1", 1000);
            e.ingest("B", "e1", 2000);
            e.ingest("A", "e1", 2500);
            e.ingest("B", "e1", 2600);
            e.ingest("C", "e1", 3000);
            // pairs: (A1,B1),(A1,B2),(A2,B2) — (A2,B1) impossible since B1 < A2
            assertEquals(3, e.matchCount("e1"), "three valid pairs complete on one C");
            e.close();
        });

        // ---- out-of-order timestamps never match backwards in time
        test("out-of-order events do not match backwards", () -> {
            MatchEngine e = MatchEngine.inMemory("t");
            e.ingest("A", "e1", 5000);
            e.ingest("B", "e1", 6000);
            e.ingest("C", "e1", 4000); // C earlier than B: not a valid chain
            assertEquals(0, e.matchCount("e1"), "C before B in event time must not match");
            e.close();
        });

        // ---- seq assignment is strictly increasing in input order
        test("seq assigned in input order", () -> {
            MatchEngine e = MatchEngine.inMemory("t");
            Event a = e.ingest("A", "e1", 100);
            Event x = e.ingest("X", "e1", 100);
            Event b = e.ingest("B", "e1", 100);
            assertEquals(1, a.seq(), "first seq");
            assertEquals(2, x.seq(), "noise also consumes a seq (it is durably logged)");
            assertEquals(3, b.seq(), "third seq");
            e.close();
        });

        // ---- expired partials are swept and cannot match later
        test("expired partials are swept", () -> {
            MatchEngine e = MatchEngine.inMemory("t");
            e.ingest("A", "e1", 1000);
            e.ingest("B", "e1", 2000);
            // this event is 11s after A -> sweeps the pending A·B pair
            e.ingest("X", "e1", 12000);
            e.ingest("C", "e1", 12500);
            assertEquals(0, e.matchCount("e1"), "swept pair must not match");
            e.close();
        });
    }

    private EngineTests() {}
}
