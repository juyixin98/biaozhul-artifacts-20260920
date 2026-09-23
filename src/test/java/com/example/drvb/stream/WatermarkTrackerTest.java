package com.example.drvb.stream;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

class WatermarkTrackerTest {

    @Test
    void tracksMaxObservedMinusDelay() {
        WatermarkTracker t = new WatermarkTracker(100);
        assertEquals(Long.MIN_VALUE, t.watermark());
        t.observe(500);
        assertEquals(400, t.watermark());
        t.observe(300); // behind, no change
        assertEquals(400, t.watermark());
        t.observe(700);
        assertEquals(600, t.watermark());
    }

    @Test
    void explicitAdvanceNeverMovesBackwards() {
        WatermarkTracker t = new WatermarkTracker(0);
        t.observe(1000);
        t.advanceTo(900);
        assertEquals(1000, t.watermark());
        t.advanceTo(1500);
        assertEquals(1500, t.watermark());
    }

    @Test
    void zeroDelayIsStrictMonotonicMax() {
        WatermarkTracker t = new WatermarkTracker(0);
        t.observe(10);
        assertEquals(10, t.watermark());
    }
}
