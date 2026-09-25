package tests;

import streammatch.engine.StreamMatcher;
import streammatch.model.EngineConfig;
import streammatch.model.EngineMode;
import streammatch.model.EngineResult;
import streammatch.model.Event;
import streammatch.model.LatePolicy;
import streammatch.model.MatchPolicy;
import streammatch.model.RemovedA;
import streammatch.time.ManualClock;
import streammatch.time.ManualScheduler;

import java.util.List;

/**
 * 处理时间模式测试。时钟与调度器全部手动注入，因此“超时先到 / 事件先到”的竞速是确定的。
 * 事件自带 timestamp 在该模式下被忽略，这里故意填不同值以验证它确实不参与判断。
 */
public class ProcessingTimeTest extends TestCase {

    private static final long W = 100L;

    private static Event e(String id, String type) {
        // 故意给每个事件不同的“假 timestamp”，PT 模式必须忽略它
        return new Event(id, "k", type, 999_999L, 0L);
    }

    private StreamMatcher engine(ManualClock clock, ManualScheduler scheduler) {
        EngineConfig cfg = new EngineConfig(EngineMode.PROCESSING_TIME, W,
                MatchPolicy.ALL_CANDIDATES, 0L, LatePolicy.DROP);
        return StreamMatcher.processingTime(cfg, clock, scheduler);
    }

    @Override
    protected void run() {
        matchInsideWindow();
        timeoutByTimer();
        boundaryLastMillis();
        cInterrupts();
        eventAfterTimeoutDoesNotMatch();
        sameInstantOrdering();
    }

    // t=0 A；t=50 B -> 匹配（忽略事件 timestamp）
    private void matchInsideWindow() {
        ManualClock clock = new ManualClock(0);
        ManualScheduler sched = new ManualScheduler();
        StreamMatcher m = engine(clock, sched);

        clock.setTime(0);
        m.process(List.of(e("A1", "A")));
        clock.setTime(50);
        EngineResult r = m.process(List.of(e("B2", "B")));
        eq(r.matches().size(), 1, "PT: 窗口内到达匹配");
        eq(r.matches().get(0).aId(), "A1", "PT: 匹配 A1");
        eq(r.matches().get(0).bId(), "B2", "PT: 匹配 B2");
        eq(r.matches().get(0).aTimestamp(), 0L, "PT: 记录的是到达时钟时间而非事件字段");
    }

    // t=0 A；推进到 t=102（触发 tA+W+1=101 的定时器）-> TIMEOUT；之后 B 无匹配
    private void timeoutByTimer() {
        ManualClock clock = new ManualClock(0);
        ManualScheduler sched = new ManualScheduler();
        StreamMatcher m = engine(clock, sched);

        m.process(List.of(e("A1", "A")));
        sched.advanceTime(101);
        clock.setTime(101);
        List<RemovedA> async = m.drainAsyncRemoved();
        eq(async.size(), 1, "PT: 定时器触发超时");
        eq(async.get(0).reason().name(), "TIMEOUT", "PT: 原因 TIMEOUT");

        clock.setTime(101);
        EngineResult r = m.process(List.of(e("B2", "B")));
        eq(r.matches().size(), 0, "PT: 超时后 B 不匹配");
    }

    // 边界：t=100 仍是窗口最后一毫秒（闭区间），B 匹配；t=101 不匹配。
    private void boundaryLastMillis() {
        ManualClock c1 = new ManualClock(0);
        ManualScheduler s1 = new ManualScheduler();
        StreamMatcher m1 = engine(c1, s1);
        m1.process(List.of(e("A1", "A")));
        c1.setTime(100);
        eq(m1.process(List.of(e("B2", "B"))).matches().size(), 1, "PT: t=100 闭区间末点匹配");

        ManualClock c2 = new ManualClock(0);
        ManualScheduler s2 = new ManualScheduler();
        StreamMatcher m2 = engine(c2, s2);
        m2.process(List.of(e("A1", "A")));
        c2.setTime(101);
        // 不显式推进调度器：依赖事件到达时的防御性清理也应判定超时
        EngineResult r = m2.process(List.of(e("B2", "B")));
        eq(r.matches().size(), 0, "PT: t=101 事件到达时防御性清理，不匹配");
        eq(r.removed().size(), 1, "PT: 防御性清理记录 TIMEOUT");
    }

    // t=0 A；t=40 C 打断；t=50 B 无匹配
    private void cInterrupts() {
        ManualClock clock = new ManualClock(0);
        ManualScheduler sched = new ManualScheduler();
        StreamMatcher m = engine(clock, sched);

        m.process(List.of(e("A1", "A")));
        clock.setTime(40);
        EngineResult cr = m.process(List.of(e("C2", "C")));
        eq(cr.removed().size(), 1, "PT: C 打断 A1");
        eq(cr.removed().get(0).cId(), "C2", "PT: 打断者 C2");
        clock.setTime(50);
        EngineResult br = m.process(List.of(e("B3", "B")));
        eq(br.matches().size(), 0, "PT: 打断后 B 无匹配");
        // 定时器应已被取消，推进时间不应再产出超时记录
        sched.advanceTime(500);
        clock.setTime(500);
        eq(m.drainAsyncRemoved().size(), 0, "PT: 被打断的 A 定时器已取消，无重复移除");
    }

    // 超时在前、C 在后：结局应记 TIMEOUT 而非 INTERRUPTED
    private void eventAfterTimeoutDoesNotMatch() {
        ManualClock clock = new ManualClock(0);
        ManualScheduler sched = new ManualScheduler();
        StreamMatcher m = engine(clock, sched);

        m.process(List.of(e("A1", "A")));
        clock.setTime(101);
        sched.advanceTime(101);
        m.drainAsyncRemoved(); // 消费超时
        clock.setTime(120);
        EngineResult r = m.process(List.of(e("C2", "C"), e("B3", "B")));
        eq(r.matches().size(), 0, "PT: 超时后 C/B 无副作用匹配");
        eq(r.removed().size(), 0, "PT: 不再重复记录移除");
    }

    // 同一时钟读数内，批内到达序决定全序：A 然后 B（同时刻）匹配；B 然后 A 不匹配
    private void sameInstantOrdering() {
        ManualClock c1 = new ManualClock(7);
        ManualScheduler s1 = new ManualScheduler();
        StreamMatcher m1 = engine(c1, s1);
        EngineResult r1 = m1.process(List.of(e("A1", "A"), e("B2", "B")));
        eq(r1.matches().size(), 1, "PT: 同刻批内 A 先于 B -> 匹配");

        ManualClock c2 = new ManualClock(7);
        ManualScheduler s2 = new ManualScheduler();
        StreamMatcher m2 = engine(c2, s2);
        EngineResult r2 = m2.process(List.of(e("B1", "B"), e("A2", "A")));
        eq(r2.matches().size(), 0, "PT: 同刻批内 B 先于 A -> 不匹配");
    }
}
