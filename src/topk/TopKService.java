package topk;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;
import java.util.Set;
import java.util.TreeSet;

/**
 * 可撤回的精确滑动窗口 TopK（按分组）。
 *
 * 语义：
 *  - 事件 = (eventId, group, key, delta, ts)，delta 可正可负。
 *  - 元素 key 的分数 = 窗口内所有活跃事件 delta 之和；净分为 0 时元素从排名中移除。
 *  - 排名：分数降序；分数相同按 key 字典序升序。
 *  - 窗口：事件在 ts > watermark - windowMs 时活跃（左开右开的上界由 watermark 决定）。
 *    watermark 只增不减，由 insert 的 ts 和查询/推进时的 now 共同推进。
 *  - 过期与撤回各自只扣减一次：事件从 activeById 移除后即失效，
 *    过期堆里的残留条目弹出时发现不在 activeById 中则跳过。
 *
 * 不做“保存全部历史再重算”：只增量维护每个分组的
 *   scores(key->score) + ranking(TreeSet) + 按 ts 排序的活跃事件堆，
 * 每次插入/撤回/过期都是 O(log n) 的增量更新。
 */
public final class TopKService {

    public enum InsertResult { APPLIED, DUPLICATE_EVENT_ID, ALREADY_EXPIRED }

    /** 排名条目：分数降序，key 升序。 */
    public record Entry(long score, String key) implements Comparable<Entry> {
        @Override
        public int compareTo(Entry o) {
            if (score != o.score) return Long.compare(o.score, score); // 分数降序
            return key.compareTo(o.key);                               // 并列按 key 升序
        }
    }

    private static final class Event {
        final String id, group, key;
        final long delta, ts;
        Event(String id, String group, String key, long delta, long ts) {
            this.id = id; this.group = group; this.key = key; this.delta = delta; this.ts = ts;
        }
    }

    private static final class GroupState {
        final Map<String, Long> scores = new HashMap<>();
        final TreeSet<Entry> ranking = new TreeSet<>();
        final PriorityQueue<Event> byTs = new PriorityQueue<>(Comparator.comparingLong(e -> e.ts));
    }

    private final long windowMs;
    private final Map<String, GroupState> groups = new HashMap<>();
    private final Map<String, Event> activeById = new HashMap<>(); // 仅活跃事件
    private final Set<String> seenEventIds = new HashSet<>();      // 所有见过的 eventId（去重）
    private long watermark = Long.MIN_VALUE;

    public TopKService(long windowMs) {
        if (windowMs <= 0) throw new IllegalArgumentException("windowMs must be > 0");
        this.windowMs = windowMs;
    }

    public long windowMs() { return windowMs; }

    public synchronized long watermark() { return watermark; }

    /** 推进逻辑时间；watermark 只增不减。 */
    public synchronized void advanceTo(long now) {
        if (now > watermark) watermark = now;
    }

    /** 插入事件。重复 eventId 拒绝；插入即过期的事件不落盘。 */
    public synchronized InsertResult insert(String eventId, String group, String key, long delta, long ts) {
        if (eventId == null || group == null || key == null)
            throw new IllegalArgumentException("eventId/group/key must not be null");
        if (!seenEventIds.add(eventId)) return InsertResult.DUPLICATE_EVENT_ID;
        advanceTo(ts);
        GroupState g = groups.computeIfAbsent(group, k -> new GroupState());
        expire(g);
        if (isExpired(ts)) return InsertResult.ALREADY_EXPIRED;
        Event e = new Event(eventId, group, key, delta, ts);
        activeById.put(eventId, e);
        g.byTs.add(e);
        applyDelta(g, key, delta);
        return InsertResult.APPLIED;
    }

    /**
     * 撤回事件。幂等：不存在 / 已撤回 / 已过期的 eventId 返回 false，不重复扣减。
     */
    public synchronized boolean retract(String eventId) {
        Event e = activeById.get(eventId);
        if (e == null) return false;
        GroupState g = groups.get(e.group);
        expire(g); // 先按当前 watermark 过期；若该事件已滑出窗口则撤回为空操作
        e = activeById.remove(eventId);
        if (e == null) return false; // 已过期，过期时只扣了一次
        applyDelta(g, e.key, -e.delta);
        return true;
    }

    /** 窗口内完整排序（rank 从 1 开始，与返回列表下标一致）。 */
    public synchronized List<Entry> ranking(String group, long now) {
        advanceTo(now);
        GroupState g = groups.get(group);
        if (g == null) return List.of();
        expire(g);
        return new ArrayList<>(g.ranking);
    }

    /** 精确 TopK：取完整排序的前 k 名；k 大于元素数时返回全部。 */
    public synchronized List<Entry> topK(String group, int k, long now) {
        if (k <= 0) return List.of();
        List<Entry> all = ranking(group, now);
        return all.size() <= k ? all : new ArrayList<>(all.subList(0, k));
    }

    private boolean isExpired(long ts) {
        return watermark != Long.MIN_VALUE && ts <= watermark - windowMs;
    }

    private void expire(GroupState g) {
        if (watermark == Long.MIN_VALUE) return;
        long threshold = watermark - windowMs;
        while (true) {
            Event e = g.byTs.peek();
            if (e == null || e.ts > threshold) break;
            g.byTs.poll();
            // 只扣减一次：仍活跃才扣；被撤回过的残留条目直接跳过
            if (activeById.remove(e.id, e)) {
                applyDelta(g, e.key, -e.delta);
            }
        }
    }

    private void applyDelta(GroupState g, String key, long delta) {
        long oldScore = g.scores.getOrDefault(key, 0L);
        if (oldScore != 0) g.ranking.remove(new Entry(oldScore, key));
        long newScore = oldScore + delta;
        if (newScore == 0) {
            g.scores.remove(key); // 净分为 0，元素移出排名
        } else {
            g.scores.put(key, newScore);
            g.ranking.add(new Entry(newScore, key));
        }
    }
}
