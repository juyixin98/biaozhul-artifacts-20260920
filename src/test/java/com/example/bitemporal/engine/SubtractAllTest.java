package com.example.bitemporal.engine;

import com.example.bitemporal.model.Interval;
import org.junit.jupiter.api.Test;

import java.time.LocalDate;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;

class SubtractAllTest {

    private static Interval i(int a, int b) {
        return Interval.of(LocalDate.of(2025, 1, a), LocalDate.of(2025, 1, b));
    }

    private static Interval open(int a) {
        return Interval.openEnded(LocalDate.of(2025, 1, a));
    }

    @Test
    void singleMiddleCutLeavesTwoRemnants() {
        // [1,20) - [5,10) = [1,5)+[10,20)
        List<Interval> out = BitemporalStore.subtractAll(i(1, 20), List.of(i(5, 10)));
        assertEquals(List.of(i(1, 5), i(10, 20)), out);
    }

    @Test
    void multipleCutsProduceGaps() {
        // [1,30) - [3,8) - [12,15) - [20,25)
        List<Interval> out = BitemporalStore.subtractAll(i(1, 30),
                List.of(i(3, 8), i(12, 15), i(20, 25)));
        assertEquals(List.of(i(1, 3), i(8, 12), i(15, 20), i(25, 30)), out);
    }

    @Test
    void abuttingCutsMergeWithoutEmptyPieces() {
        // [1,20) - [1,10) - [10,20)：相接切口完全覆盖，无残余
        List<Interval> out = BitemporalStore.subtractAll(i(1, 20),
                List.of(i(1, 10), i(10, 20)));
        assertEquals(List.of(), out);
    }

    @Test
    void cutOnOpenEndedSourceStaysOpen() {
        // [10,+inf) - [15,20) = [10,15)+[20,+inf)
        List<Interval> out = BitemporalStore.subtractAll(open(10), List.of(i(15, 20)));
        assertEquals(2, out.size());
        assertEquals(i(10, 15), out.get(0));
        assertEquals(open(20), out.get(1));
    }

    @Test
    void openEndedCutConsumesTail() {
        // [1,30) - [15,+inf) = [1,15)
        List<Interval> out = BitemporalStore.subtractAll(i(1, 30), List.of(open(15)));
        assertEquals(List.of(i(1, 15)), out);
    }

    @Test
    void nonOverlappingCutsAreIgnored() {
        List<Interval> out = BitemporalStore.subtractAll(i(1, 10), List.of(i(12, 20)));
        assertEquals(List.of(i(1, 10)), out);
    }
}
