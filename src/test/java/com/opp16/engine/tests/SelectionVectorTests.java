package com.opp16.engine.tests;

import com.opp16.engine.EngineException;
import com.opp16.engine.SelectionVector;

import java.util.Arrays;

public final class SelectionVectorTests {

    private SelectionVectorTests() {}

    public static void run() {
        Assert.suite("SelectionVector");

        SelectionVector sv = new SelectionVector(10);
        Assert.check("new vector empty", sv.isEmpty());
        sv.append(3);
        sv.append(7);
        sv.append(3); // duplicate
        sv.append(9);
        Assert.eq("size after appends", sv.size(), 4);
        Assert.eq("duplicate index retained twice",
                Arrays.toString(sv.toArray()), Arrays.toString(new int[]{3, 7, 3, 9}));
        Assert.eq("get preserves order", sv.get(2), 3);

        Assert.throwsEngineEx("negative subscript rejected", "invalid selection subscript -1",
                () -> sv.append(-1));
        Assert.throwsEngineEx("subscript == rowCount rejected", "invalid selection subscript 10",
                () -> sv.append(10));
        Assert.throwsEngineEx("subscript far out of range rejected", "invalid selection subscript 1000000",
                () -> sv.append(1_000_000));
        Assert.eq("failed append did not grow vector", sv.size(), 4);

        // appendMask: bit p maps to row base+p
        SelectionVector sv2 = new SelectionVector(200);
        sv2.appendMask(0b10101L, 100, 5); // positions 0,2,4 -> rows 100,102,104
        Assert.eq("appendMask maps bits to rows",
                Arrays.toString(sv2.toArray()), Arrays.toString(new int[]{100, 102, 104}));

        // mask reaching the last valid row is fine; one bit beyond would throw
        SelectionVector sv3 = new SelectionVector(132);
        long mask = (1L << 3) | (1L << 63); // rows base+3 and base+63
        sv3.appendMask(mask, 68, 64);      // maps to 71 and 131 (last valid)
        Assert.eq("mask up to last valid row accepted", sv3.size(), 2);

        SelectionVector sv4 = new SelectionVector(130);
        Assert.throwsEngineEx("mask bit beyond rowCount rejected", "invalid selection subscript 130",
                () -> sv4.appendMask(1L << 2, 128, 64));

        // bulk validation
        Assert.throwsEngineEx("fromIndices rejects bad middle index", "invalid selection subscript 50",
                () -> SelectionVector.fromIndices(10, new int[]{1, 2, 50, 3}));

        // many repeated indices stress the grow path
        SelectionVector sv5 = new SelectionVector(2);
        for (int i = 0; i < 1000; i++) sv5.append(i % 2);
        Assert.eq("1000 repeated indices all stored", sv5.size(), 1000);
        Assert.eq("odd positions all point at row 1", sv5.get(999), 1);
    }
}
