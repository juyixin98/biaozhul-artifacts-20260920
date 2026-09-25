package com.example.cptx.tests;

import com.example.cptx.core.CheckpointPolicy;
import com.example.cptx.core.Clock;
import com.example.cptx.core.Event;
import com.example.cptx.core.Json;
import com.example.cptx.core.KeyedAggregate;
import com.example.cptx.core.MutableClock;
import com.example.cptx.core.Pipeline;

import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/** 基础单元测试：JSON、聚合算子、无故障管线、可注入时钟与时间策略。 */
public final class CoreTest {

    private CoreTest() {}

    public static void run(TestRunner t) throws Exception {
        json(t);
        aggregate(t);
        pipelineHappyPath(t);
        timePolicy(t);
    }

    private static void json(TestRunner t) {
        t.section("Json 解析/写回/中文与转义");
        String s = "{\"name\":\"测试 \\u0041 \\n\",\"n\":-12,\"d\":1.5e2,\"b\":[true,false,null,{},[]]}";
        Object parsed = Json.parse(s);
        Map<String, Object> m = Json.obj(parsed);
        t.eq(Json.str(m.get("name")), "测试 A \n", "unicode+转义解析");
        t.eq(Json.lng(m.get("n")), -12L, "负整数为 Long");
        t.eq(Json.dbl(m.get("d")), 150.0, "指数小数为 Double");
        t.eq(Json.arr(m.get("b")).size(), 5, "数组长度");
        String again = Json.write(Json.parse(Json.write(parsed)));
        t.eq(again, Json.write(parsed), "往返一致");
        t.check(Json.pretty(Json.parse("{\"b\":1,\"a\":2}")).contains("\"a\""), "Map 按 key 排序输出");

        boolean threw = false;
        try { Json.parse("{bad}"); } catch (IllegalArgumentException e) { threw = true; }
        t.check(threw, "非法 JSON 抛异常");
    }

    private static void aggregate(TestRunner t) {
        t.section("KeyedAggregate 计数/求和");
        KeyedAggregate agg = new KeyedAggregate();
        agg.process(new Event(0, "A", 1.5));
        agg.process(new Event(1, "B", 2.0));
        agg.process(new Event(2, "A", 0.5));
        Map<String, Map<String, Object>> snap = agg.snapshot();
        t.eq(Json.lng(snap.get("A").get("count")), 2L, "A count");
        t.approxEq(Json.dbl(snap.get("A").get("sum")), 2.0, 1e-9, "A sum");
        t.eq(Json.lng(snap.get("B").get("count")), 1L, "B count");

        KeyedAggregate restored = new KeyedAggregate();
        restored.restore(snap);
        restored.process(new Event(3, "B", 3.0));
        t.approxEq(Json.dbl(restored.snapshot().get("B").get("sum")), 5.0, 1e-9, "恢复后继续聚合 B");
    }

    private static void pipelineHappyPath(TestRunner t) throws Exception {
        t.section("无故障管线：偏移推进与按条数检查点");
        Path dir = TestDirs.create("core-happy");
        Clock fixed = () -> 1234L;
        Pipeline p = new Pipeline(dir, CheckpointPolicy.count(5), fixed, null);
        p.appendAndProcess(TestFixtures.payloads(12));
        t.eq(p.nextOffset(), 12L, "处理 12 条后 nextOffset=12");
        t.eq(p.lastCheckpointId(), 2L, "每 5 条触发 -> 已完成 2 个检查点");
        p.flushCheckpoint();
        t.eq(p.lastCheckpointId(), 3L, "尾部强制第 3 个检查点");

        Map<String, Object> st = p.status();
        t.eq(st.get("nextOffset"), 12L, "status nextOffset");
        Map<?, ?> summary = Json.obj(st.get("summary"));
        t.eq(Json.obj(summary.get("A")).get("count"), TestFixtures.expectedCount(12, "A"), "汇总表 A 条数");
    }

    private static void timePolicy(TestRunner t) throws Exception {
        t.section("可注入时间：MutableClock 驱动按时间触发的检查点");
        Path dir = TestDirs.create("core-time");
        MutableClock clock = new MutableClock(1000);
        Pipeline p = new Pipeline(dir, CheckpointPolicy.wallTime(clock, 500), clock, null);
        List<Map<String, Object>> events = TestFixtures.payloads(6);
        // 逐条喂入，第 3 条后推进时间超过 500ms 应触发一次
        for (int i = 0; i < 3; i++) {
            p.appendAndProcess(events.subList(i, i + 1));
        }
        t.eq(p.lastCheckpointId(), 0L, "时间未到不触发");
        clock.advance(500);
        p.appendAndProcess(events.subList(3, 4));
        t.eq(p.lastCheckpointId(), 1L, "时间到达后在下一条事件处触发");
        t.eq(p.nextOffset(), 4L, "nextOffset 随处理推进");
        p.appendAndProcess(events.subList(4, 6));
        t.eq(p.lastCheckpointId(), 1L, "周期已重置，未再触发");
        clock.advance(500);
        p.appendAndProcess(List.of());
        p.flushCheckpoint();
        t.eq(p.lastCheckpointId(), 2L, "手动 flush 补上尾部检查点");
    }
}
