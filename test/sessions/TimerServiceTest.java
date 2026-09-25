package sessions;

import java.util.ArrayList;
import java.util.List;

import sessions.testing.Assert;
import sessions.testing.Test;
import sessions.time.SimTimerService;

/** {@link SimTimerService}：水位线单调性、触发边界（严格大于）、同刻 FIFO、回调中注册新定时器。 */
public final class TimerServiceTest {

    @Test("水位线单调不减，回退被忽略")
    static void monotonicWatermark() {
        SimTimerService ts = new SimTimerService();
        ts.advanceWatermark(10);
        ts.advanceWatermark(5);
        Assert.assertEquals(10L, ts.currentWatermark(), "watermark must not go backwards");
    }

    @Test("定时器在 W >= fireTime 时触发（闭区间，与 <=gap 合并规则一致）")
    static void triggerBoundary() {
        SimTimerService ts = new SimTimerService();
        List<String> fired = new ArrayList<>();
        ts.registerTimer(10, () -> fired.add("t10"));
        ts.advanceWatermark(9);
        Assert.assertTrue(fired.isEmpty(), "timer at 10 must not fire at W=9");
        ts.advanceWatermark(10);
        Assert.assertEquals(List.of("t10"), fired, "timer at 10 fires at W=10 (inclusive)");
    }

    @Test("同一时刻的定时器按注册顺序触发")
    static void sameTimeFifo() {
        SimTimerService ts = new SimTimerService();
        List<String> fired = new ArrayList<>();
        ts.registerTimer(5, () -> fired.add("first"));
        ts.registerTimer(5, () -> fired.add("second"));
        ts.advanceWatermark(6);
        Assert.assertEquals(List.of("first", "second"), fired, "FIFO at same fire time");
    }

    @Test("不同时刻按时间顺序触发")
    static void orderedByTime() {
        SimTimerService ts = new SimTimerService();
        List<String> fired = new ArrayList<>();
        ts.registerTimer(9, () -> fired.add("nine"));
        ts.registerTimer(3, () -> fired.add("three"));
        ts.registerTimer(7, () -> fired.add("seven"));
        ts.advanceWatermark(10);
        Assert.assertEquals(List.of("three", "seven", "nine"), fired, "time ordered");
    }

    @Test("严格定时器在 W > fireTime 时才触发（状态清除边界）")
    static void strictAfterBoundary() {
        SimTimerService ts = new SimTimerService();
        List<String> fired = new ArrayList<>();
        ts.registerTimerAfter(10, () -> fired.add("purge"));
        ts.advanceWatermark(10);
        Assert.assertTrue(fired.isEmpty(), "strict timer at 10 must not fire at W=10");
        ts.advanceWatermark(11);
        Assert.assertEquals(List.of("purge"), fired, "strict timer at 10 fires at W=11");
    }

    @Test("同刻先封窗（闭区间）后清除（严格）的相对顺序")
    static void sealBeforePurgeOrderingAtSameTime() {
        SimTimerService ts = new SimTimerService();
        List<String> fired = new ArrayList<>();
        // 同一 fireTime=10：闭区间定时器与严格定时器
        ts.registerTimer(10, () -> fired.add("seal"));
        ts.registerTimerAfter(10, () -> fired.add("purge"));
        ts.advanceWatermark(10);
        Assert.assertEquals(List.of("seal"), fired, "at W=10 only inclusive timer fires");
        ts.advanceWatermark(11);
        Assert.assertEquals(List.of("seal", "purge"), fired, "strict timer fires next");
    }

    @Test("回调中新注册且已到点的定时器在同一轮触发")
    static void nestedRegistration() {
        SimTimerService ts = new SimTimerService();
        List<String> fired = new ArrayList<>();
        ts.registerTimer(5, () -> {
            fired.add("outer");
            ts.registerTimer(6, () -> fired.add("inner"));
        });
        ts.advanceWatermark(10);
        Assert.assertEquals(List.of("outer", "inner"), fired,
                "nested timer registered at fire-time 6 fires within advance to 10");
    }

    @Test("推进正无穷触发全部定时器（收尾语义）")
    static void infinityFiresAll() {
        SimTimerService ts = new SimTimerService();
        List<String> fired = new ArrayList<>();
        ts.registerTimer(Long.MAX_VALUE - 1, () -> fired.add("huge"));
        ts.advanceWatermark(Long.MAX_VALUE);
        Assert.assertEquals(List.of("huge"), fired, "W=+inf fires all timers");
    }
}
