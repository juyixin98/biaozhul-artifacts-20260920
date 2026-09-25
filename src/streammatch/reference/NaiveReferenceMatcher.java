package streammatch.reference;

import streammatch.model.Event;
import streammatch.model.MatchPolicy;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 小数据精确参考实现（离线、暴力法）。
 *
 * <p>输入为<b>完整</b>事件集合，按全序 {@code (timestamp, seq)} 排序后逐 key 独立计算。
 * 它不模拟队列与 watermark，而是直接枚举候选对，目的是与流式引擎相互印证：
 * 流式引擎按 {@code (timestamp, seq)} 顺序重放（replay）时，结果必须与本实现逐对一致。
 *
 * <h3>ALL_CANDIDATES 的集合定义</h3>
 * 对同 key、全序位置 i &lt; j 上的 A 与 B，当且仅当
 * {@code 0 <= tB - tA <= windowMillis} 且位置区间 (i, j) 内不存在 C 时，
 * (A_i, B_j) 为一个匹配。逐对枚举、按全序上的 (j, i) 排序输出。
 *
 * <h3>SKIP_PAST_LAST 的离线定义</h3>
 * 全序扫描：维护未消费且未超时的 A；B 取其中最早的一个产出匹配并清空其余；
 * C 清空它之前的所有等待 A（排序输入下即清空全部等待）。
 */
public final class NaiveReferenceMatcher {

    /** 参考实现产出的匹配（不含引擎内部 seq/emitIndex，只保留核对所需字段）。 */
    public record RefMatch(String key, String aId, String bId, long aTimestamp, long bTimestamp) {
    }

    public record RefRemoved(String key, String aId, String reason, String cId) {
    }

    public record ReferenceResult(List<RefMatch> matches, List<RefRemoved> removed) {
    }

    private NaiveReferenceMatcher() {
    }

    /**
     * @param events       完整事件集合；事件的 {@code seq} 必须已按期望的到达全序赋值
     *                     （重放场景：重放器先按 {@code (timestamp, 原始到达序)} 排序再赋 seq）
     * @param windowMillis 窗口 W（闭区间 [0, W]）
     * @param policy       重叠策略
     */
    public static ReferenceResult compute(List<Event> events, long windowMillis, MatchPolicy policy) {
        if (windowMillis <= 0) {
            throw new IllegalArgumentException("windowMillis must be > 0");
        }
        Map<String, List<Event>> byKey = new LinkedHashMap<>();
        for (Event e : events) {
            byKey.computeIfAbsent(e.key(), k -> new ArrayList<>()).add(e);
        }

        List<RefMatch> matches = new ArrayList<>();
        List<RefRemoved> removed = new ArrayList<>();
        for (Map.Entry<String, List<Event>> entry : byKey.entrySet()) {
            String key = entry.getKey();
            List<Event> sorted = new ArrayList<>(entry.getValue());
            sorted.sort(Comparator.comparingLong(Event::timestamp).thenComparingLong(Event::seq));
            if (policy == MatchPolicy.ALL_CANDIDATES) {
                computeAll(key, sorted, windowMillis, matches, removed);
            } else {
                computeSkip(key, sorted, windowMillis, matches, removed);
            }
        }
        // 跨 key 的输出顺序：参考实现按 key 字典序 + 全序输出；引擎按“到达时刻”交错输出。
        // 匹配是事件集的确定函数，测试按集合（或按 key 分组）核对，不比较跨 key 的交错顺序。
        return new ReferenceResult(matches, removed);
    }

    private static void computeAll(String key, List<Event> es, long w,
                                   List<RefMatch> matches, List<RefRemoved> removed) {
        int n = es.size();
        boolean[] matchedOnce = new boolean[n];

        // 匹配：按 B 的全序位置 j 外层、A 的位置 i 内层穷举候选对，与任何状态机无关
        for (int j = 0; j < n; j++) {
            Event b = es.get(j);
            if (!Event.B.equals(b.type())) {
                continue;
            }
            for (int i = 0; i < j; i++) {
                Event a = es.get(i);
                if (!Event.A.equals(a.type())) {
                    continue;
                }
                long delta = b.timestamp() - a.timestamp();
                if (delta < 0 || delta > w) {
                    continue;
                }
                boolean blockedByC = false;
                for (int k = i + 1; k < j; k++) {
                    if (Event.C.equals(es.get(k).type())) {
                        blockedByC = true;
                        break;
                    }
                }
                if (!blockedByC) {
                    matches.add(new RefMatch(key, a.id(), b.id(), a.timestamp(), b.timestamp()));
                    matchedOnce[i] = true;
                }
            }
        }

        // 移除结局：用一次独立的全序扫描得出（watermark L=0 语义），仅服务于可观测性核对
        List<Integer> pending = new ArrayList<>(); // es 下标
        for (int p = 0; p < n; p++) {
            Event e = es.get(p);
            long t = e.timestamp();
            List<Integer> alive = new ArrayList<>();
            for (int idx : pending) {
                Event a = es.get(idx);
                if (t > a.timestamp() + w) {
                    removed.add(new RefRemoved(key, a.id(),
                            matchedOnce[idx] ? "EXPIRED_AFTER_MATCH" : "TIMEOUT", null));
                } else {
                    alive.add(idx);
                }
            }
            pending = alive;
            if (Event.A.equals(e.type())) {
                pending.add(p);
            } else if (Event.C.equals(e.type())) {
                for (int idx : pending) {
                    removed.add(new RefRemoved(key, es.get(idx).id(), "INTERRUPTED_BY_C", e.id()));
                }
                pending.clear();
            }
            // B 不清理：ALL 模式下 A 可重复匹配
        }
        // 注意：输入结束时仍存活的 A 不记 TIMEOUT——流式语义里它们是“仍在等待”，
        // 调用方显式把 watermark 推进到无穷（或足够大）后才超时。匹配集合不受此影响。
    }

    private static void computeSkip(String key, List<Event> es, long w,
                                    List<RefMatch> matches, List<RefRemoved> removed) {
        // 全序扫描的等待 A 列表（保持顺序）
        List<Event> pending = new ArrayList<>();
        for (Event e : es) {
            long t = e.timestamp();
            // watermark(L=0) 推进到 t 后，严格超时（t > tA+W）的 A 先移除
            List<Event> survivors = new ArrayList<>();
            for (Event a : pending) {
                if (t > a.timestamp() + w) {
                    removed.add(new RefRemoved(key, a.id(), "TIMEOUT", null));
                } else {
                    survivors.add(a);
                }
            }
            pending = survivors;

            switch (e.type()) {
                case Event.A -> pending.add(e);
                case Event.C -> {
                    for (Event a : pending) {
                        removed.add(new RefRemoved(key, a.id(), "INTERRUPTED_BY_C", e.id()));
                    }
                    pending.clear();
                }
                case Event.B -> {
                    if (!pending.isEmpty()) {
                        Event chosen = pending.get(0); // 排序后存活者中最早者必在窗内
                        matches.add(new RefMatch(key, chosen.id(), e.id(), chosen.timestamp(), t));
                        for (int idx = 1; idx < pending.size(); idx++) {
                            removed.add(new RefRemoved(key, pending.get(idx).id(), "SKIPPED_AFTER_MATCH", null));
                        }
                        pending.clear();
                    }
                }
                default -> throw new AssertionError(e.type());
            }
        }
        // 同 ALL：末尾存活的等待 A 不记 TIMEOUT（仍在等待，等显式 watermark 收尾）
    }
}
