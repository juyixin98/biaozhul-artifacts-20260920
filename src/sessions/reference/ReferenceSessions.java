package sessions.reference;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import sessions.agg.AggregateFunction;
import sessions.model.Event;
import sessions.model.Window;

/**
 * 小数据量的"精确参考实现"：离线对每个 key 的全部事件按时间排序后做完整分组。
 *
 * <p>分组规则与流式算子一致：排序后相邻事件间隔 {@code <= gap} 归入同一会话。
 * 该实现不关心水位线与迟到——它就是"事后全知"的正确答案。
 * 对照方法：把流式 changelog 折叠成最终表后，与本实现对<b>同一批事件</b>
 * （流式侧通常喂入"未被门控丢弃"的事件）的结果逐条比较。
 */
public final class ReferenceSessions {

    /** 一个参考分组结果：窗口 + 聚合值。 */
    public record Result(String key, Window window, long aggregate) {
    }

    private ReferenceSessions() {
    }

    /** 对全量事件做离线分组（不做任何迟到丢弃）。 */
    public static List<Result> groupAll(List<Event> events, long gap, AggregateFunction<?> agg) {
        Map<String, List<Event>> byKey = new LinkedHashMap<>();
        for (Event e : events) {
            byKey.computeIfAbsent(e.key(), k -> new ArrayList<>()).add(e);
        }
        List<Result> out = new ArrayList<>();
        for (Map.Entry<String, List<Event>> entry : byKey.entrySet()) {
            groupOneKey(entry.getKey(), entry.getValue(), gap, agg, out);
        }
        return out;
    }

    static void groupOneKey(String key, List<Event> keyEvents, long gap,
                            AggregateFunction<?> agg, List<Result> out) {
        List<Event> sorted = new ArrayList<>(keyEvents);
        sorted.sort(Comparator.comparingLong(Event::timestamp));

        long start = 0;
        long prev = 0;
        long value = agg.emptyResult();
        boolean inWindow = false;

        for (Event e : sorted) {
            if (!inWindow) {
                start = e.timestamp();
                value = add(agg, value, e.value());
                inWindow = true;
            } else if (e.timestamp() - prev <= gap) {
                value = add(agg, value, e.value());
            } else {
                out.add(new Result(key, new Window(start, prev), value));
                start = e.timestamp();
                value = add(agg, agg.emptyResult(), e.value());
            }
            prev = e.timestamp();
        }
        if (inWindow) {
            out.add(new Result(key, new Window(start, prev), value));
        }
    }

    @SuppressWarnings("unchecked")
    private static long add(AggregateFunction<?> agg, long result, long v) {
        return ((AggregateFunction<Object>) agg).add(result, v);
    }
}
