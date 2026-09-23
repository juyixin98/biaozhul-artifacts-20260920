package dedup.tests;

import dedup.core.DedupResult;
import dedup.core.Event;
import dedup.json.Json;
import dedup.stream.StreamProcessor;
import dedup.time.Clock;

/**
 * 验收场景 1：同 ID 不同载荷。
 *
 * <ul>
 *   <li>事件身份是 (key,id)，不是 (id,payload)：同一 ID 即使载荷不同也必须抑制；</li>
 *   <li>载荷不一致要留下可观察标记 payloadMismatch；</li>
 *   <li>键顺序不同但语义相同的 JSON 载荷必须视为一致（规范化哈希）。</li>
 * </ul>
 */
final class SameIdDifferentPayloadTest {

    private SameIdDifferentPayloadTest() {}

    static void register(TestFramework tf) {
        tf.run("同ID不同载荷：以ID为身份抑制，标记payloadMismatch，承诺范围内零重复输出", a -> {
            var config = new StreamProcessor.Config(5_000, 1000, "manual", 0, 1000);
            var clock = Clock.manual(0);
            var proc = StreamProcessor.create(config, clock, null);

            Event first = event("k", "evt-1", 10_000,
                    "{\"amount\":100,\"currency\":\"CNY\"}");
            Event dupDifferentPayload = event("k", "evt-1", 10_000,
                    "{\"amount\":999,\"currency\":\"CNY\"}");
            // 键顺序与首次不同但语义相同，且 100 与 100.0 数值相等
            Event dupCanonicalEqual = event("k", "evt-1", 10_000,
                    "{\"currency\":\"CNY\",\"amount\":100.0}");
            // 载荷里多一个嵌套对象，明确不同
            Event dupExtraField = event("k", "evt-1", 10_000,
                    "{\"amount\":100,\"currency\":\"CNY\",\"meta\":{\"src\":\"retry\"}}");

            DedupResult r1 = proc.process(first);
            DedupResult r2 = proc.process(dupDifferentPayload);
            DedupResult r3 = proc.process(dupCanonicalEqual);
            DedupResult r4 = proc.process(dupExtraField);

            a.eq(r1.decision(), DedupResult.Decision.ACCEPT, "首次事件应 ACCEPT");
            a.check(r1.dedupGuaranteed(), "首次事件应在承诺范围内");

            a.eq(r2.decision(), DedupResult.Decision.SUPPRESS, "不同载荷的重复必须抑制");
            a.check(r2.payloadMismatch(), "不同载荷必须置 payloadMismatch=true");
            a.check(r2.dedupGuaranteed(), "抑制应发生在承诺范围内");

            a.eq(r3.decision(), DedupResult.Decision.SUPPRESS, "规范化后相等的载荷也应抑制");
            a.check(!r3.payloadMismatch(), "键序不同/100 与 100.0 不应判为载荷不一致");

            a.eq(r4.decision(), DedupResult.Decision.SUPPRESS, "多出字段的重复仍以 ID 为准抑制");
            a.check(r4.payloadMismatch(), "多出字段应判为载荷不一致");

            // 承诺范围内零重复输出：只有第一条被发射
            var emitted = proc.drainOutputs(0, false);
            a.eqLong(emitted.size(), 1, "输出缓冲中应恰好 1 条");
            a.eq(emitted.get(0).event().id(), "evt-1", "发射的应是首条事件");
            a.eqLong(proc.statsJson().get("accepted") instanceof Json.Num n ? n.value().longValueExact() : -1,
                    1, "accepted=1");
        });

        tf.run("同ID不同载荷：不同key下相同ID是不同事件（复合身份）", a -> {
            var config = StreamProcessor.Config.defaults();
            var proc = StreamProcessor.create(config, Clock.manual(0), null);

            DedupResult r1 = proc.process(event("user-a", "id-9", 1000, "{\"v\":1}"));
            DedupResult r2 = proc.process(event("user-b", "id-9", 1000, "{\"v\":2}"));

            a.eq(r1.decision(), DedupResult.Decision.ACCEPT, "user-a|id-9 首次接受");
            a.eq(r2.decision(), DedupResult.Decision.ACCEPT, "user-b|id-9 是不同身份，应接受");
            a.eqLong(proc.drainOutputs(0, false).size(), 2, "两条都应输出");
        });
    }

    private static Event event(String key, String id, long eventTime, String payloadJson) {
        return new Event(key, id, eventTime, Json.parse(payloadJson));
    }
}
