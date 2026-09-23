package dedup.tests;

import dedup.core.DedupResult;
import dedup.core.Event;
import dedup.json.Json;
import dedup.stream.StreamProcessor;
import dedup.time.Clock;

/**
 * 验收场景 4：极迟重复（窗口外旧重复不可保证去重）。
 *
 * <ol>
 *   <li>事件首次到达并接受；</li>
 *   <li>水位线越过其事件时间 + allowedLateness，墓碑按承诺释放（timeEvicted>0）；</li>
 *   <li>同一 ID 的副本极迟到达：系统<b>不承诺</b>去重 —— 返回 UNGUARANTEED、
 *       透传输出、{@code dedupGuaranteed=false}，这就是明确、可观察的语义边界；</li>
 *   <li>边界两侧（恰好窗口内 / 恰好窗口外）逐一核对；</li>
 *   <li>硬上限强制淘汰导致的墓碑消失走同一条可观察路径。</li>
 * </ol>
 */
final class VeryLateDuplicateTest {

    private VeryLateDuplicateTest() {}

    static void register(TestFramework tf) {
        tf.run("极迟重复：水位线释放墓碑后旧重复透传，dedupGuaranteed=false 可观察", a -> {
            long lateness = 5_000;
            var config = new StreamProcessor.Config(lateness, 1000, "manual", 0, 1000);
            var proc = StreamProcessor.create(config, Clock.manual(0), null);

            // 首次事件，事件时间 10000
            Event first = ev("late-id", 10_000);
            DedupResult r1 = proc.process(first);
            a.eq(r1.decision(), DedupResult.Decision.ACCEPT, "首次接受");
            a.eqLong(proc.deduplicator().activeTombstones(), 1, "墓碑已写入");

            // 水位线推到 14999：窗口下界 9999，eventTime=10000 仍在窗口内（>=）
            proc.setManualWatermark(14_999);
            a.eqLong(proc.deduplicator().activeTombstones(), 1,
                    "水位线 14999 时墓碑仍保留（边界 inclusive）");

            // 水位线推到 15000：下界 10000；释放条件 eventTime < 10000，仍保留
            proc.setManualWatermark(15_000);
            a.eqLong(proc.deduplicator().activeTombstones(), 1,
                    "水位线 15000 时 eventTime=10000 仍在边界内");
            a.eq(proc.process(ev("late-id", 10_000)).decision(),
                    DedupResult.Decision.SUPPRESS, "边界点重复仍承诺抑制");

            // 水位线推到 15001：下界 10001，eventTime=10000 < 10001 -> 墓碑释放
            proc.setManualWatermark(15_001);
            a.eqLong(proc.deduplicator().activeTombstones(), 0,
                    "越出窗口后墓碑必须释放");
            a.eqLong(((Json.Num) proc.statsJson().get("timeEvicted")).value().longValueExact(),
                    1, "timeEvicted 计数为 1");

            // 极迟副本：无法保证去重 —— 透传 + 标志位
            DedupResult veryLate = proc.process(ev("late-id", 10_000));
            a.eq(veryLate.decision(), DedupResult.Decision.UNGUARANTEED,
                    "窗口外重复应为 UNGUARANTEED");
            a.check(veryLate.emitted(), "UNGUARANTEED 事件必须透传输出");
            a.check(!veryLate.dedupGuaranteed(),
                    "dedupGuaranteed 必须为 false（明确的可观察承诺边界）");
            a.check(!veryLate.payloadMismatch(), "载荷是否一致已无从判断，不应误报 mismatch");

            // 可观察计数
            a.eqLong(((Json.Num) proc.statsJson().get("unguarded")).value().longValueExact(),
                    1, "unguarded 计数为 1");

            // 输出缓冲：首次 + 极迟透传，共 2 条；其中仅 1 条 dedupGuaranteed
            var outputs = proc.drainOutputs(0, false);
            a.eqLong(outputs.size(), 2, "首次与极迟透传共 2 条输出");
            long guaranteed = outputs.stream()
                    .filter(o -> o.result().dedupGuaranteed()).count();
            a.eqLong(guaranteed, 1, "承诺范围内的输出恰好 1 条（无重复）");
            long unguardedEmitted = outputs.stream()
                    .filter(o -> !o.result().dedupGuaranteed()).count();
            a.eqLong(unguardedEmitted, 1, "不可保证输出 1 条，下游可按标志区分");
        });

        tf.run("极迟重复：乱序流中同ID首副本在窗口内时，任何eventTime副本都承诺去重", a -> {
            var config = new StreamProcessor.Config(5_000, 1000, "manual", 0, 1000);
            var proc = StreamProcessor.create(config, Clock.manual(0), null);

            a.eq(proc.process(ev("x", 20_000)).decision(), DedupResult.Decision.ACCEPT, "首副本接受");
            proc.setManualWatermark(22_000); // 下界 17000，墓碑 eventTime=20000 保留
            a.eq(proc.process(ev("x", 18_000)).decision(), DedupResult.Decision.SUPPRESS,
                    "窗口内同ID副本（即使eventTime不同）必须抑制");
            a.check(proc.process(ev("x", 18_000)).dedupGuaranteed(),
                    "抑制发生在承诺范围内");
        });

        tf.run("极迟重复：硬上限强制淘汰同样走 dedupGuaranteed=false 的可观察路径", a -> {
            var config = new StreamProcessor.Config(1_000_000, 3, "manual", 0, 1000);
            var proc = StreamProcessor.create(config, Clock.manual(0), null);

            // 不推进水位线，仅靠容量：4 个不同 ID 超过 maxTombstones=3
            proc.process(ev("a", 1));
            proc.process(ev("b", 2));
            proc.process(ev("c", 3));
            proc.process(ev("d", 4)); // 触发强制淘汰（降到 90% -> target=2）

            long forced = ((Json.Num) proc.statsJson().get("forcedEvicted")).value().longValueExact();
            a.check(forced >= 1, "必须发生强制淘汰, forcedEvicted=" + forced);

            // 被淘汰的最旧 ID 再来副本：无法保证（可能与未淘汰 ID 共存，只检查被淘汰的 a/b 之一）
            // a(eventTime=1) 一定被淘汰：其重复必须返回 UNGUARANTEED
            DedupResult ghost = proc.process(ev("a", 1));
            a.eq(ghost.decision(), DedupResult.Decision.UNGUARANTEED,
                    "强制淘汰后旧ID副本必须标记不可保证");
            a.check(!ghost.dedupGuaranteed(), "dedupGuaranteed=false");
        });
    }

    private static Event ev(String id, long t) {
        return new Event(null, id, t, Json.Nul.INSTANCE);
    }
}
