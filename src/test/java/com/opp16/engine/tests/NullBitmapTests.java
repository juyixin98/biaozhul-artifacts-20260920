package com.opp16.engine.tests;

import com.opp16.engine.NullBitmap;

public final class NullBitmapTests {

    private NullBitmapTests() {}

    public static void run() {
        Assert.suite("NullBitmap");

        NullBitmap empty = new NullBitmap(0);
        Assert.eq("empty bitmap counts zero present", empty.countPresent(), 0);

        NullBitmap allNull = new NullBitmap(130);
        Assert.eq("fresh bitmap is all null (130 rows)", allNull.countPresent(), 0);
        Assert.check("row 0 null by default", allNull.isPresent(0) == false);
        Assert.check("row 129 null by default", allNull.isPresent(129) == false);

        NullBitmap all = NullBitmap.allPresent(130);
        Assert.eq("allPresent counts 130", all.countPresent(), 130);
        Assert.check("allPresent row 63", all.isPresent(63));
        Assert.check("allPresent row 64 (word boundary)", all.isPresent(64));
        Assert.check("allPresent row 129", all.isPresent(129));

        all.setNull(64);
        Assert.eq("nulling one row decrements count", all.countPresent(), 129);
        Assert.check("row 64 now null", !all.isPresent(64));
        all.setPresent(64);
        Assert.eq("restoring row returns count", all.countPresent(), 130);

        // presentMask across word boundaries
        NullBitmap bm = NullBitmap.allPresent(140);
        long mask = bm.presentMask(60, 10); // rows 60..69 cross the 64-word edge
        Assert.eq("cross-word mask has 10 bits", Long.bitCount(mask), 10);
        Assert.check("cross-word bit for row 60", (mask & 1L) != 0);
        Assert.check("cross-word bit for row 69", (mask & (1L << 9)) != 0);

        long tail = bm.presentMask(136, 4);
        Assert.eq("last partial word gives 4 bits", Long.bitCount(tail), 4);

        bm.setNull(65);
        long m2 = bm.presentMask(64, 4); // rows 64 T, 65 F, 66 T, 67 T -> bits 0,2,3
        Assert.eq("mask == 0b1101", m2, 0b1101L);

        long fullWord = NullBitmap.allPresent(64).presentMask(0, 64);
        Assert.check("64-wide mask is all ones", fullWord == ~0L);
    }
}
