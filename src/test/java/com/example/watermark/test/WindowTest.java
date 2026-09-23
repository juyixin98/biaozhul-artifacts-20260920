package com.example.watermark.test;

import static com.example.watermark.test.Assert.assertEquals;
import static com.example.watermark.test.Assert.assertTrue;

import java.util.ArrayList;
import java.util.List;

import com.example.watermark.test.TestRunner.Test;
import com.example.watermark.watermarks.LateEvent;
import com.example.watermark.watermarks.StreamEvent;
import com.example.watermark.windowing.TumblingWindowProcessor;
import com.example.watermark.windowing.WindowResult;

/**
 * Exact-reference checks for the event-time tumbling window: close boundary,
 * per-partition windows, empty windows not firing, late events not admitted.
 */
public class WindowTest {

    @Test
    public void windowsCloseWhenWatermarkReachesEnd() {
        TumblingWindowProcessor wp = new TumblingWindowProcessor(1000);
        List<WindowResult> closed = new ArrayList<>();
        wp.addWindowCloseListener(closed::add);

        wp.onEvent(new StreamEvent(0, "p", "a"));
        wp.onEvent(new StreamEvent(999, "p", "b"));
        wp.onEvent(new StreamEvent(1000, "p", "c"));

        wp.onWatermark(500);
        assertEquals(0, closed.size(), "[0,1000) not closed at wm=500");

        wp.onWatermark(999);
        assertEquals(0, closed.size(), "[0,1000) not closed at wm=999");

        wp.onWatermark(1000);
        assertEquals(1, closed.size(), "[0,1000) closes at wm=1000");
        WindowResult r = closed.get(0);
        assertEquals(0, r.windowStart());
        assertEquals(1000, r.windowEnd());
        assertEquals(2, r.eventCount(), "a and b only; 1000 belongs to next window");
        assertEquals(List.of("a", "b"), r.payloads());

        wp.onWatermark(2000);
        assertEquals(2, closed.size(), "[1000,2000) closes");
        assertEquals(1, closed.get(1).eventCount(), "only c");
    }

    @Test
    public void windowsArePartitionedByKey() {
        TumblingWindowProcessor wp = new TumblingWindowProcessor(100);
        List<WindowResult> closed = new ArrayList<>();
        wp.addWindowCloseListener(closed::add);

        wp.onEvent(new StreamEvent(10, "x", 1));
        wp.onEvent(new StreamEvent(20, "y", 2));
        wp.onWatermark(100);
        assertEquals(2, closed.size(), "one window per partition");
        assertEquals("x", closed.get(0).partitionKey());
        assertEquals("y", closed.get(1).partitionKey());
    }

    @Test
    public void emptyWindowsNeverFire() {
        TumblingWindowProcessor wp = new TumblingWindowProcessor(100);
        List<WindowResult> closed = new ArrayList<>();
        wp.addWindowCloseListener(closed::add);

        wp.onEvent(new StreamEvent(500, "p", "z"));
        wp.onWatermark(1000);
        assertEquals(1, closed.size(), "only the non-empty [500,600) fires, no empty earlier ones");
        assertEquals(500, closed.get(0).windowStart());
    }

    @Test
    public void lateEventsAreReportedAndNotAdmitted() {
        TumblingWindowProcessor wp = new TumblingWindowProcessor(100);
        List<LateEvent> dropped = new ArrayList<>();
        wp.addDroppedLateListener(dropped::add);

        wp.onEvent(new StreamEvent(0, "p", 1));
        wp.onWatermark(100); // closes [0,100)

        // A late event for the now-closed window is reported.
        LateEvent late = new LateEvent(new StreamEvent(50, "p", 999), 100, 0, false);
        wp.onLateEvent(late);
        assertEquals(1, dropped.size(), "late event into closed window reported");
        assertEquals(1, wp.getResults().size(), "no extra window result");
        assertTrue(wp.getResults().stream().noneMatch(r -> r.payloads().contains(999)),
                "late payload never enters a window result");

        // A late event whose window is still open is admitted nowhere either:
        // manager-side semantics say late == not processed.
        LateEvent late2 = new LateEvent(new StreamEvent(150, "p", 888), 150, 0, false);
        wp.onLateEvent(late2);
        assertEquals(1, dropped.size(), "[100,200) not yet closed at wm=150: not 'dropped', "
                + "but still not admitted");
    }
}
