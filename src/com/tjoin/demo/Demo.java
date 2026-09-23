package com.tjoin.demo;

import com.tjoin.core.BufferCapacityExceededException;
import com.tjoin.core.Event;
import com.tjoin.core.JoinConfig;
import com.tjoin.core.JoinPair;
import com.tjoin.core.IntervalJoinOperator;
import com.tjoin.core.StreamSide;
import com.tjoin.time.BoundedOutOfOrdernessWatermarks;
import com.tjoin.time.ManualProcessingTimeService;
import com.tjoin.time.PeriodicWatermarkAssigner;

import java.util.ArrayList;
import java.util.List;

/**
 * 命令行演示：覆盖验收关注点（无需 HTTP 即可运行）。
 *
 * <pre>
 *   java com.tjoin.demo.Demo
 * </pre>
 *
 * 场景：
 * <ol>
 *   <li>一侧停滞：右水位线不动时，左缓冲中可匹配的旧记录不被提前丢弃；</li>
 *   <li>区间边界：闭/开边界在 tsDiff 恰好等于下界/上界时的行为；</li>
 *   <li>重复值 &amp; 重复投递：值可重复，ID 去重，每对只输出一次；</li>
 *   <li>缓冲上限：一侧停滞且对侧持续到达，达到上限即快速失败；</li>
 *   <li>可注入时间驱动：手动处理时间服务 + 周期水位线发射。</li>
 * </ol>
 */
public final class Demo {

    private static int failures = 0;

    public static void main(String[] args) {
        stalledSideKeepsMatchableRecords();
        intervalBoundaries();
        duplicateValuesAndRedelivery();
        bufferCapWhileOppositeStalled();
        injectedTimeDrivesPeriodicWatermarks();

        System.out.println();
        if (failures == 0) {
            System.out.println("[DEMO] ALL SCENARIOS PASSED");
        } else {
            System.out.println("[DEMO] " + failures + " CHECK(S) FAILED");
            System.exit(1);
        }
    }

    private static Event e(String id, StreamSide side, String key, long ts, Object value) {
        return new Event(id, side, key, ts, value);
    }

    private static void check(boolean cond, String what) {
        if (cond) {
            System.out.println("  PASS: " + what);
        } else {
            failures++;
            System.out.println("  FAIL: " + what);
        }
    }

    /** 场景 1：右流停滞（水位线不推进），左事件不得被提前清理。 */
    private static void stalledSideKeepsMatchableRecords() {
        System.out.println("== Scenario 1: one side stalls, matchable records are retained ==");
        IntervalJoinOperator op = new IntervalJoinOperator(JoinConfig.symmetric(5L));
        List<JoinPair> out = new ArrayList<>();

        op.processEvent(e("L1", StreamSide.LEFT, "k", 10, "v"), out::add);
        op.processEvent(e("L2", StreamSide.LEFT, "k", 12, "v"), out::add);
        op.processWatermark(StreamSide.LEFT, 12); // 只有左水位线推进
        System.out.println("  left wm=12, right wm=MIN_VALUE; buffered left="
                + op.bufferedCount(StreamSide.LEFT));
        check(op.bufferedCount(StreamSide.LEFT) == 2, "right stall => left records retained");
        check(out.isEmpty(), "no outputs before any right event");

        // 右流恢复：边界上的事件（ts=15，L1+5）仍必须能匹配
        op.processEvent(e("R1", StreamSide.RIGHT, "k", 15, "v"), out::add);
        check(out.size() == 2 && out.stream().anyMatch(p -> p.left().id().equals("L1")),
                "R1@15 joins both L1@10 (upper boundary) and L2@12");

        // 右水位线推进到 16：L1 上界 15 < 16 → L1 过期；L2 上界 17 → 保留
        op.processWatermark(StreamSide.RIGHT, 16);
        check(op.bufferedCount(StreamSide.LEFT) == 1, "L1 expired only after right wm passes 15");
        System.out.println();
    }

    /** 场景 2：区间边界闭/开语义。 */
    private static void intervalBoundaries() {
        System.out.println("== Scenario 2: interval boundary semantics ==");
        JoinConfig closed = JoinConfig.builder().lowerBound(-2L).upperBound(2L).build();
        IntervalJoinOperator op = new IntervalJoinOperator(closed);
        List<JoinPair> out = new ArrayList<>();
        op.processEvent(e("L", StreamSide.LEFT, "k", 10, "v"), out::add);
        op.processEvent(e("Rlo", StreamSide.RIGHT, "k", 8, "v"), out::add);  // diff -2 下界
        op.processEvent(e("Rmid", StreamSide.RIGHT, "k", 10, "v"), out::add); // diff 0
        op.processEvent(e("Rhi", StreamSide.RIGHT, "k", 12, "v"), out::add);  // diff +2 上界
        check(out.size() == 3, "inclusive bounds: diff in {-2,0,+2} all match");

        JoinConfig open = JoinConfig.builder()
                .lowerBound(-2L).upperBound(2L)
                .lowerInclusive(false).upperInclusive(false)
                .build();
        IntervalJoinOperator op2 = new IntervalJoinOperator(open);
        List<JoinPair> out2 = new ArrayList<>();
        op2.processEvent(e("L", StreamSide.LEFT, "k", 10, "v"), out2::add);
        op2.processEvent(e("Rlo", StreamSide.RIGHT, "k", 8, "v"), out2::add);
        op2.processEvent(e("Rmid", StreamSide.RIGHT, "k", 10, "v"), out2::add);
        op2.processEvent(e("Rhi", StreamSide.RIGHT, "k", 12, "v"), out2::add);
        check(out2.size() == 1, "exclusive bounds: only diff 0 matches");
        System.out.println();
    }

    /** 场景 3：允许重复值；重复 ID 投递忽略；每对仅输出一次。 */
    private static void duplicateValuesAndRedelivery() {
        System.out.println("== Scenario 3: duplicate values allowed, unique IDs dedup pairs ==");
        IntervalJoinOperator op = new IntervalJoinOperator(JoinConfig.symmetric(10L));
        List<JoinPair> out = new ArrayList<>();
        op.processEvent(e("L1", StreamSide.LEFT, "k", 10, "same-value"), out::add);
        op.processEvent(e("L2", StreamSide.LEFT, "k", 11, "same-value"), out::add); // 值重复
        op.processEvent(e("R1", StreamSide.RIGHT, "k", 10, "same-value"), out::add);
        int n1 = out.size();
        check(n1 == 2, "duplicate values both join: " + n1 + " pairs");

        // 同一事件重放两次
        op.processEvent(e("L1", StreamSide.LEFT, "k", 10, "changed-value"), out::add);
        op.processEvent(e("R1", StreamSide.RIGHT, "k", 10, "changed-value"), out::add);
        check(out.size() == n1, "redelivered IDs produce no extra pairs");
        check(op.metrics().duplicates() == 2, "duplicate counter = 2");
        check(out.stream().distinct().count() == out.size(), "every emitted pair unique");
        System.out.println();
    }

    /** 场景 4：右流停滞，左流持续到达且设置缓冲上限 → 超限快速失败。 */
    private static void bufferCapWhileOppositeStalled() {
        System.out.println("== Scenario 4: buffer cap bounds memory when opposite side stalls ==");
        JoinConfig cfg = JoinConfig.builder()
                .lowerBound(0L).upperBound(100L)
                .maxBufferedPerSide(3)
                .build();
        IntervalJoinOperator op = new IntervalJoinOperator(cfg);
        boolean threw = false;
        try {
            for (int i = 0; i < 5; i++) {
                op.processEvent(e("L" + i, StreamSide.LEFT, "k", 10 + i, "v"), p -> { });
            }
        } catch (BufferCapacityExceededException ex) {
            threw = true;
            System.out.println("  got expected: " + ex.getMessage());
        }
        check(threw, "capacity exceeded after 3 buffered left events");
        check(op.bufferedCount(StreamSide.LEFT) == 3, "buffer stayed bounded at 3");
        System.out.println();
    }

    /** 场景 5：手动时钟/调度驱动周期水位线（零真实等待，确定性）。 */
    private static void injectedTimeDrivesPeriodicWatermarks() {
        System.out.println("== Scenario 5: injected processing time drives periodic watermarks ==");
        JoinConfig cfg = JoinConfig.symmetric(5L);
        IntervalJoinOperator op = new IntervalJoinOperator(cfg);
        ManualProcessingTimeService timer = new ManualProcessingTimeService(0L);
        BoundedOutOfOrdernessWatermarks gen = new BoundedOutOfOrdernessWatermarks(0L);
        PeriodicWatermarkAssigner assigner =
                new PeriodicWatermarkAssigner(StreamSide.LEFT, gen, op, timer, 100L);
        assigner.start();

        List<JoinPair> out = new ArrayList<>();
        gen.onEvent(e("L1", StreamSide.LEFT, "k", 10, "v"));
        op.processEvent(e("L1", StreamSide.LEFT, "k", 10, "v"), out::add);
        check(op.watermark(StreamSide.LEFT) == Long.MIN_VALUE, "no wm before first tick after event");

        int fired = timer.advanceBy(100L); // t=100：触发一次
        check(fired == 1 && op.watermark(StreamSide.LEFT) == 10L,
                "tick at +100ms emits left wm=10");

        gen.onEvent(e("L2", StreamSide.LEFT, "k", 20, "v"));
        op.processEvent(e("L2", StreamSide.LEFT, "k", 20, "v"), out::add);
        timer.advanceBy(100L); // t=200
        check(op.watermark(StreamSide.LEFT) == 20L, "next tick emits left wm=20");

        // 右水位线一直没动 → L1/L2 都保留
        check(op.bufferedCount(StreamSide.LEFT) == 2, "right side still stalls, nothing expired");
        assigner.close();
        check(timer.pendingCount() == 0, "periodic task cancelled on close");
        System.out.println();
    }
}
