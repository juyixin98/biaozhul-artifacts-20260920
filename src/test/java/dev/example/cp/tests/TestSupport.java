package dev.example.cp.tests;

import dev.example.cp.core.Event;
import dev.example.cp.engine.CheckpointScheduler;
import dev.example.cp.engine.Clock;
import dev.example.cp.engine.Engine;
import dev.example.cp.storage.SourceLog;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/** 测试公共工具：标准事件集与“连续执行基线”。 */
final class TestSupport {

    private TestSupport() {
    }

    /**
     * 10 条确定性事件（key 在 a/b/c 间循环，value 从 1 递增）。
     * 最终汇总：a=22, b=15, c=18；最终偏移=9。
     */
    static final List<Event> EVENTS = List.of(
            Event.of(0, "a", 1), Event.of(1, "b", 2), Event.of(2, "c", 3),
            Event.of(3, "a", 4), Event.of(4, "b", 5), Event.of(5, "c", 6),
            Event.of(6, "a", 7), Event.of(7, "b", 8), Event.of(8, "c", 9),
            Event.of(9, "a", 10));

    static final TreeMap<String, Long> FINAL_SUMS = new TreeMap<>(Map.of("a", 22L, "b", 15L, "c", 18L));
    static final long FINAL_OFFSET = 9L;

    static void seed(Path dir) {
        new SourceLog(dir).appendAll(EVENTS);
    }

    /** 无故障连续执行基线：消费全部 10 条并在结束提交。 */
    static Engine.Status runBaseline(Path dir) {
        Engine e = Engine.open(dir, new dev.example.cp.engine.ManualScheduler(), Clock.SYSTEM);
        e.runUntilDrainedAndCommitted();
        return e.status();
    }

    static Engine open(Path dir, CheckpointScheduler scheduler) {
        return Engine.open(dir, scheduler, Clock.SYSTEM);
    }

    /** 手动构造 n 条事件（需要不同于标准集时使用）。 */
    static List<Event> cycleEvents(int n) {
        List<Event> list = new ArrayList<>();
        String[] keys = {"a", "b", "c"};
        for (int i = 0; i < n; i++) {
            list.add(Event.of(i, keys[i % 3], i + 1));
        }
        return list;
    }
}
