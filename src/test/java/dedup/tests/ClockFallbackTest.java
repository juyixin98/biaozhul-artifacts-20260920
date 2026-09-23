package dedup.tests;

import dedup.core.DedupResult;
import dedup.core.Event;
import dedup.stream.StreamProcessor;
import dedup.time.Clock;
import dedup.time.DeterministicScheduler;
import dedup.time.TaskScheduler;

/**
 * 验收场景 2：时钟回退。
 *
 * <ul>
 *   <li>手动时钟回拨后再触发周期任务，bounded 模式水位线不会倒退（只由观测到的
 *       最大事件时间决定），调度结果确定；</li>
 *   <li>manual 模式显式提交更小的水位线被拒绝（advanced=false）；</li>
 *   <li>回退期间到达的乱序/迟到事件在承诺窗口内仍能去重。</li>
 * </ul>
 */
final class ClockFallbackTest {

    private ClockFallbackTest() {}

    static void register(TestFramework tf) {
        tf.run("时钟回退：bounded模式周期触发不产生倒退水位线，回退期间窗口内重复仍抑制", a -> {
            // outOfOrderness=2000：水位线 = 观测最大事件时间 - 2000
            var config = new StreamProcessor.Config(5_000, 1000, "bounded", 2_000, 1000);
            var clock = Clock.manual(0);
            var proc = StreamProcessor.create(config, clock, null);
            var scheduler = TaskScheduler.deterministic(clock);
            // 每 1000ms 一次周期 tick；advanceAndRun 保证“推进多少时间就触发多少次”
            scheduler.schedulePeriodic(1_000, 1_000, proc::tick);

            // 推进到 12000（事件时间域）之前先让处理时钟走到 12000，期间 tick 无数值不前进
            scheduler.advanceAndRun(12_000);
            proc.process(evt("e1", 12_000));
            scheduler.advanceAndRun(1_000); // t=13000 触发 tick -> 水位线 10000
            a.eqLong(proc.deduplicator().currentWatermark(), 10_000,
                    "首个 tick 后水位线应为 12000-2000=10000");

            // 处理一个较晚事件把观测最大值推到 20000，再推进 1000ms tick -> 18000
            proc.process(evt("e2", 20_000));
            scheduler.advanceAndRun(1_000); // t=14000
            a.eqLong(proc.deduplicator().currentWatermark(), 18_000,
                    "水位线推进到 18000");

            // === 时钟回退 ===：处理机器时钟从 14000 跳回 3000（外部 NTP 回退模型）。
            // 处理器（含水位线状态）保持不变，只用回退后的时钟新建调度器并触发 tick。
            var fallenClock = Clock.manual(3_000);
            var fallenScheduler = TaskScheduler.deterministic(fallenClock);
            fallenScheduler.schedulePeriodic(0, 1_000, proc::tick);
            fallenScheduler.runDue(); // t=3000，补跑 3 次 tick
            a.eqLong(proc.deduplicator().currentWatermark(), 18_000,
                    "时钟回退后 tick 不得拉低水位线");

            // 回退期间到达一条“旧”事件（事件时间 16000 < 已观测最大值 20000，但仍在
            // 水位线 18000 的承诺窗口 [13000, +∞) 内）—— 首次接受；重复副本必须抑制
            DedupResult late = proc.process(evt("e3", 16_000));
            DedupResult lateDup = proc.process(evt("e3", 16_000));
            a.eq(late.decision(), DedupResult.Decision.ACCEPT, "回退期窗口内新事件接受");
            a.eq(lateDup.decision(), DedupResult.Decision.SUPPRESS, "回退期窗口内重复仍抑制");
            a.check(!lateDup.payloadMismatch(), "载荷一致不应标记不一致");

            // 旧事件也不能改变水位线：回退时钟再 tick 仍是 18000
            fallenScheduler.advanceAndRun(1_000);
            a.eqLong(proc.deduplicator().currentWatermark(), 18_000,
                    "旧事件不得改变水位线");
        });

        tf.run("时钟回退：manual模式提交更小水位线被拒绝", a -> {
            var config = new StreamProcessor.Config(5_000, 1000, "manual", 0, 1000);
            var proc = StreamProcessor.create(config, Clock.manual(0), null);

            a.check(proc.setManualWatermark(20_000), "水位线首次推进应成功");
            a.eqLong(proc.deduplicator().currentWatermark(), 20_000, "水位线=20000");
            a.check(!proc.setManualWatermark(15_000), "回退到 15000 必须被拒绝");
            a.eqLong(proc.deduplicator().currentWatermark(), 20_000, "拒绝后水位线保持 20000");
            a.check(!proc.setManualWatermark(20_000), "重复提交相同水位线不应算推进");
            a.check(proc.setManualWatermark(20_001), "前进 1ms 应被接受");
        });

        tf.run("时钟回退：水位线回退不会错误释放/复活墓碑，已抑制ID保持抑制", a -> {
            var config = new StreamProcessor.Config(5_000, 1000, "manual", 0, 1000);
            var proc = StreamProcessor.create(config, Clock.manual(0), null);

            proc.process(evt("dup", 30_000));      // 接受
            proc.setManualWatermark(32_000);      // 窗口下界 27000，墓碑保留
            a.eq(proc.process(evt("dup", 30_000)).decision(),
                    DedupResult.Decision.SUPPRESS, "窗口内重复抑制");

            proc.setManualWatermark(10_000);      // 试图回退 —— 拒绝
            a.eq(proc.process(evt("dup", 30_000)).decision(),
                    DedupResult.Decision.SUPPRESS, "回退被拒绝后，墓碑仍然存在并继续抑制");
        });
    }

    private static Event evt(String id, long t) {
        return new Event(null, id, t, dedup.json.Json.Nul.INSTANCE);
    }
}
