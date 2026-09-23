package topk;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;
import java.util.TreeSet;

/**
 * 单个分组的滑动窗口状态。
 *
 * <h2>核心不变量</h2>
 * <ol>
 *   <li>只保存窗口内仍然有效的事件（{@code ts > watermark - windowMs}），
 *       不保存全部历史；过期事件在滑出时立即从内存移除。</li>
 *   <li>每条事件的增量对 item 总分恰好生效一次：过期滑出与主动撤回互斥，
 *       撤回后的事件进入墓碑表，墓碑表只保留到该事件本应过期的时刻
 *       （因此墓碑表大小同样受“一个窗口”约束，不会随历史无限增长）。</li>
 *   <li>{@link #ranked} 始终是窗口内“至少有一条有效事件”的 item 的精确全序：
 *       分数降序，分数相同按 itemId 字典序升序。TopK 直接取前 K，无需重算。</li>
 * </ol>
 *
 * <p>所有公开方法都在实例锁内执行（服务端按分组加锁，线程安全）。
 */
public final class GroupState {

    /** 插入结果。 */
    public enum InsertStatus {
        /** 成功并入窗口。 */
        INSERTED,
        /** eventId 在窗口内已存在（含已撤回但仍在墓碑保留期内），拒绝。 */
        DUPLICATE,
        /** 事件时间已落在窗口左边界之外（迟到事件），拒绝且不占内存。 */
        LATE
    }

    /** 撤回结果。 */
    public enum RetractStatus {
        /** 成功撤回，增量已扣减。 */
        RETRACTED,
        /** 事件此前已撤回（墓碑仍在保留期内），本次为幂等空操作，不重复扣减。 */
        ALREADY_RETRACTED,
        /** 事件不存在或已过期滑出窗口，无法撤回。 */
        EVENT_UNKNOWN
    }

    /** TopK 结果行。 */
    public static final class Row {
        public final String itemId;
        public final long score;

        public Row(String itemId, long score) {
            this.itemId = itemId;
            this.score = score;
        }

        @Override
        public String toString() {
            return itemId + ":" + score;
        }
    }

    private final long windowMs;

    /** 单调水位：事件时间 {@code <= watermark - windowMs} 视为过期。null 表示尚未推进。 */
    private Long watermark = null;

    /** 窗口内有效事件（已撤回的会立刻删除）。 */
    private final Map<String, Event> events = new HashMap<>();

    /** 有效事件的 item 总分。 */
    private final Map<String, Long> scores = new HashMap<>();

    /** 每个 item 当前的有效事件数；为 0 时该 item 退出排名。 */
    private final Map<String, Integer> counts = new HashMap<>();

    /** 精确全序集合：分数降序、itemId 升序。 */
    private final TreeSet<String> ranked = new TreeSet<>(this::compareItems);

    /** 按 (ts, eventId) 排序的过期队列；惰性删除，过期即弹。 */
    private final PriorityQueue<Event> expiry =
            new PriorityQueue<>(Comparator.comparingLong((Event e) -> e.ts).thenComparing(e -> e.eventId));

    /** 已撤回事件的墓碑：eventId -> 撤回时间（即事件时间戳），只保留到事件本应过期。 */
    private final Map<String, Long> tombstones = new HashMap<>();

    public GroupState(long windowMs) {
        if (windowMs <= 0) {
            throw new IllegalArgumentException("windowMs 必须为正数: " + windowMs);
        }
        this.windowMs = windowMs;
    }

    public long windowMs() {
        return windowMs;
    }

    public synchronized Long watermark() {
        return watermark;
    }

    private int compareItems(String a, String b) {
        // 注意：该比较器依赖可变的 scores，任何分数变更都必须先从 ranked 摘除再改分再放回。
        long sa = scores.getOrDefault(a, 0L);
        long sb = scores.getOrDefault(b, 0L);
        if (sa != sb) {
            return Long.compare(sb, sa); // 分数降序
        }
        return a.compareTo(b); // 同分按 ID 升序
    }

    /** 推进水位并滑出过期事件 / 清理过期墓碑。 */
    public synchronized void advance(long ts) {
        if (watermark == null || ts > watermark) {
            watermark = ts;
        }
        evict();
    }

    private void evict() {
        if (watermark == null) {
            return;
        }
        long horizon = watermark - windowMs; // 事件 ts <= horizon 过期
        while (true) {
            Event e = expiry.peek();
            if (e == null || e.ts > horizon) {
                break;
            }
            expiry.poll();
            Event live = events.remove(e.eventId);
            if (live != null) {
                applyDelta(live.itemId, -live.delta, -1);
            }
            // 已撤回事件不在 events 中：其增量在撤回时已经扣过，这里绝不能再扣。
        }
        // 墓碑惰性清理：撤回的事件过了它本应过期的时刻即可删除，eventId 随后可复用。
        tombstones.entrySet().removeIf(en -> en.getValue() <= horizon);
    }

    /**
     * 插入事件。
     *
     * @return 状态枚举；{@link InsertStatus#LATE} / {@link InsertStatus#DUPLICATE} 时无任何状态变更。
     */
    public synchronized InsertStatus insert(Event e) {
        if (watermark != null && e.ts <= watermark - windowMs) {
            return InsertStatus.LATE;
        }
        if (events.containsKey(e.eventId) || tombstones.containsKey(e.eventId)) {
            return InsertStatus.DUPLICATE;
        }
        events.put(e.eventId, e);
        expiry.add(e);
        applyDelta(e.itemId, e.delta, 1);
        return InsertStatus.INSERTED;
    }

    /**
     * 撤回事件（幂等）。
     *
     * @param ts 撤回请求时间；会先推进水位。早于等于窗口左边界的事件视为已过期，无法撤回。
     */
    public synchronized RetractStatus retract(String eventId, long ts) {
        advance(ts);
        if (tombstones.containsKey(eventId)) {
            return RetractStatus.ALREADY_RETRACTED;
        }
        Event e = events.remove(eventId);
        if (e == null) {
            return RetractStatus.EVENT_UNKNOWN;
        }
        // 不从 expiry 队列主动删除（PriorityQueue.remove 是 O(n)）：采用惰性删除，
        // 让它留在队列中，到期弹出时在 events 里已找不到，自然不会重复扣减。
        // 队列长度仍只以一个窗口为界（撤回事件最晚在它本应过期时被弹出）。
        applyDelta(e.itemId, -e.delta, -1);
        tombstones.put(eventId, e.ts);
        evict(); // 若该事件恰好在边界上（advance 后本就应过期），墓碑立即清掉
        return RetractStatus.RETRACTED;
    }

    /** 查询当前窗口 TopK（不足 K 个则返回全部；含 0 分与负分 item）。 */
    public synchronized List<Row> topK(int k) {
        if (k < 0) {
            throw new IllegalArgumentException("k 不能为负: " + k);
        }
        List<Row> out = new ArrayList<>(Math.min(k, ranked.size()));
        int n = 0;
        for (String item : ranked) {
            if (n++ >= k) {
                break;
            }
            out.add(new Row(item, scores.get(item)));
        }
        return out;
    }

    /** 窗口内完整排序（测试对照用：TopK 必须与其前 K 项完全一致）。 */
    public synchronized List<Row> fullOrder() {
        List<Row> out = new ArrayList<>(ranked.size());
        for (String item : ranked) {
            out.add(new Row(item, scores.get(item)));
        }
        return out;
    }

    /**
     * 施加一个增量并维护精确全序。
     *
     * @param itemId 条目
     * @param delta  分数增量（撤回/过期时为原增量的相反数）
     * @param unit   有效事件数变化：插入 +1，撤回/过期 -1
     */
    private void applyDelta(String itemId, long delta, int unit) {
        Integer oldCount = counts.get(itemId);
        int nc = (oldCount == null ? 0 : oldCount) + unit;
        if (nc < 0) {
            throw new IllegalStateException("item 有效事件计数变为负: " + itemId);
        }
        boolean present = oldCount != null && oldCount > 0;
        if (present) {
            ranked.remove(itemId); // 分数变化前先摘除，避免比较器读到变动中的分数
        }
        long ns = scores.getOrDefault(itemId, 0L) + delta;
        if (nc == 0) {
            scores.remove(itemId);
            counts.remove(itemId);
        } else {
            scores.put(itemId, ns);
            counts.put(itemId, nc);
            if (!present) {
                ranked.add(itemId);
            }
        }
        if (present && nc > 0) {
            ranked.add(itemId);
        }
    }

    // ---------------- 可观测性（测试/排障用） ----------------

    public synchronized int activeEventCount() {
        return events.size();
    }

    public synchronized int activeItemCount() {
        return ranked.size();
    }

    public synchronized int tombstoneCount() {
        return tombstones.size();
    }

    public synchronized long scoreOf(String itemId) {
        return scores.getOrDefault(itemId, 0L);
    }

    public synchronized Map<String, Object> snapshot() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("windowMs", windowMs);
        m.put("watermark", watermark);
        m.put("activeEvents", events.size());
        m.put("activeItems", ranked.size());
        m.put("tombstones", tombstones.size());
        m.put("expiryQueue", expiry.size());
        List<Map<String, Object>> rows = new ArrayList<>();
        for (Row r : fullOrder()) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("itemId", r.itemId);
            row.put("score", r.score);
            rows.add(row);
        }
        m.put("items", rows);
        return m;
    }
}
