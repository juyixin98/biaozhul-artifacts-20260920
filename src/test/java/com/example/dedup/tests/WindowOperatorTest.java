package com.example.dedup.tests;

import com.example.dedup.json.Json;
import com.example.dedup.model.Event;
import com.example.dedup.model.WindowResult;
import com.example.dedup.watermark.WatermarkGenerator;
import com.example.dedup.window.TumblingWindowOperator;

import java.util.List;

/** Unit tests for tumbling windows: folding, firing time, late drops. */
public final class WindowOperatorTest {

    public static void register(TestRunner r) {
        r.run("window: buckets by floor(ts/size)", () -> {
            var w = new TumblingWindowOperator(1000, 0);
            TestRunner.assertEquals(0L, w.windowStart(0), "floor");
            TestRunner.assertEquals(0L, w.windowStart(999), "floor");
            TestRunner.assertEquals(1000L, w.windowStart(1000), "boundary");
            TestRunner.assertEquals(-1000L, w.windowStart(-1), "negative time floors down");
        });

        r.run("window: fires only at end+lateness watermark", () -> {
            var w = new TumblingWindowOperator(1000, 100);
            w.add(Event.upsert("e1", 100, "k", TestData.payload("v", 1)));
            TestRunner.assertEquals(0, w.fire(1099).size(), "not eligible yet");
            List<WindowResult> fired = w.fire(1100);
            TestRunner.assertEquals(1, fired.size(), "fires at wm=end+lateness");
            WindowResult wr = fired.get(0);
            TestRunner.assertEquals(0L, wr.windowStart(), "start");
            TestRunner.assertEquals(1000L, wr.windowEnd(), "end");
            TestRunner.assertEquals("k", wr.key(), "key");
            TestRunner.assertEquals(1L, wr.upsertCount(), "one upsert");
            TestRunner.assertEquals("e1", wr.lastUpsertId(), "id");
        });

        r.run("window: late event after firing is reported, not folded", () -> {
            var w = new TumblingWindowOperator(1000, 0);
            w.add(Event.upsert("e1", 100, "k", null));
            w.fire(1000); // window fired
            TestRunner.assertTrue(w.isWindowLate(500, 1000), "500 is in fired window");
            TestRunner.assertFalse(w.isWindowLate(1500, 1000), "1500 is next window");
        });

        r.run("window: deletedByTombstone when last event is delete", () -> {
            var w = new TumblingWindowOperator(1000, 0);
            w.add(Event.upsert("u", 100, "k", TestData.payload("v", 1)));
            w.add(Event.delete("d", 200, "k"));
            WindowResult wr = w.fire(1000).get(0);
            TestRunner.assertTrue(wr.deletedByTombstone(), "delete after upsert");
        });

        r.run("window: not deleted when upsert comes after delete", () -> {
            var w = new TumblingWindowOperator(1000, 0);
            w.add(Event.delete("d", 100, "k"));
            w.add(Event.upsert("u", 200, "k", TestData.payload("v", 2)));
            WindowResult wr = w.fire(1000).get(0);
            TestRunner.assertFalse(wr.deletedByTombstone(), "upsert after delete wins");
            TestRunner.assertEquals(1L, wr.deleteCount(), "delete counted");
        });

        r.run("window: multiple keys fire independently and in order", () -> {
            var w = new TumblingWindowOperator(1000, 0);
            w.add(Event.upsert("a", 100, "ka", null));
            w.add(Event.upsert("b", 200, "kb", null));
            List<WindowResult> fired = w.fire(1000);
            TestRunner.assertEquals(2, fired.size(), "two keys");
            TestRunner.assertEquals("ka", fired.get(0).key(), "keys sorted");
        });

        r.run("window: multiple windows fire on one watermark advance", () -> {
            var w = new TumblingWindowOperator(1000, 0);
            w.add(Event.upsert("a", 100, "k", null));
            w.add(Event.upsert("b", 1500, "k", null));
            List<WindowResult> fired = w.fire(3000);
            TestRunner.assertEquals(2, fired.size(), "two windows fired");
            TestRunner.assertEquals(0L, fired.get(0).windowStart(), "w0 first");
            TestRunner.assertEquals(1000L, fired.get(1).windowStart(), "w1 second");
        });

        r.run("window: NO_WATERMARK never fires", () -> {
            var w = new TumblingWindowOperator(1000, 0);
            w.add(Event.upsert("a", 100, "k", null));
            TestRunner.assertEquals(0, w.fire(WatermarkGenerator.NO_WATERMARK).size(), "no wm");
        });

        r.run("window: snapshot/restore then fire matches", () -> {
            var w = new TumblingWindowOperator(1000, 0);
            w.add(Event.upsert("a", 100, "k", Json.obj()));
            var snap = w.snapshot();
            var w2 = new TumblingWindowOperator(1000, 0);
            w2.restore((com.example.dedup.json.Json.JsonObject) snap);
            WindowResult wr = w2.fire(1000).get(0);
            TestRunner.assertEquals("a", wr.lastUpsertId(), "restored accumulator");
        });
    }
}
