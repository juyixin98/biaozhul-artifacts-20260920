package com.example.sessionwindow.tests;

import com.example.sessionwindow.model.Event;
import com.example.sessionwindow.engine.WatermarkGenerator;

import static com.example.sessionwindow.tests.Assert.assertEquals;

public class WatermarkGeneratorTest {

    @Test
    void tracksMaxMinusOutOfOrderness() {
        WatermarkGenerator gen = new WatermarkGenerator(3);
        assertEquals(7L, gen.onEvent(Event.of("k", 10)), "first event: 10-3=7");
        assertEquals(Long.MIN_VALUE, gen.onEvent(Event.of("k", 9)), "older event: no change");
    }

    @Test
    void advancesOnlyOnNewMaximum() {
        WatermarkGenerator gen = new WatermarkGenerator(0);
        assertEquals(10L, gen.onEvent(Event.of("k", 10)), "wm 10");
        assertEquals(Long.MIN_VALUE, gen.onEvent(Event.of("k", 4)), "late event: no change");
        assertEquals(Long.MIN_VALUE, gen.onEvent(Event.of("k", 10)), "same ts: no change");
        assertEquals(20L, gen.onEvent(Event.of("k", 20)), "wm 20");
        assertEquals(20L, gen.currentWatermark(), "current 20");
    }

    @Test
    void appliesOutOfOrdernessLag() {
        WatermarkGenerator gen = new WatermarkGenerator(5);
        assertEquals(5L, gen.onEvent(Event.of("k", 10)), "10-5=5");
        // 12-5=7 > 5 -> advances
        assertEquals(7L, gen.onEvent(Event.of("k", 12)), "12-5=7");
    }
}
