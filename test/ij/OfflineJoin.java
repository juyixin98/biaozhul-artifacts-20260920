package ij;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.TreeSet;

/**
 * 离线全量连接参考实现：不依赖流式引擎的任何状态逻辑，
 * 直接对两侧完整事件集做同 key 的嵌套匹配 + 排序，作为正确性的独立基准。
 *
 * 配对条件与引擎一致：l.ts + lowerBound <= r.ts <= l.ts + upperBound（闭区间）。
 */
final class OfflineJoin {

    static final class RefEvent {
        final String id;
        final String key;
        final long ts;

        RefEvent(String id, String key, long ts) {
            this.id = id;
            this.key = key;
            this.ts = ts;
        }
    }

    /** 规范化配对标识：key:leftId@leftTs-rightId@rightTs（按字典序），保留同时间不同事件。 */
    static TreeSet<String> fullOuterPairs(List<RefEvent> lefts, List<RefEvent> rights,
                                          long lowerBound, long upperBound) {
        List<RefEvent> l = new ArrayList<>(lefts);
        List<RefEvent> r = new ArrayList<>(rights);
        l.sort(Comparator.comparing((RefEvent e) -> e.key).thenComparing(e -> e.ts));
        r.sort(Comparator.comparing((RefEvent e) -> e.key).thenComparing(e -> e.ts));

        TreeSet<String> pairs = new TreeSet<>();
        for (RefEvent le : l) {
            for (RefEvent re : r) {
                int cmp = le.key.compareTo(re.key);
                if (cmp < 0) {
                    break; // 右列表已按 key 排序，后面的 key 更大
                }
                if (cmp > 0) {
                    continue;
                }
                long d = re.ts - le.ts;
                if (d >= lowerBound && d <= upperBound) {
                    pairs.add(le.key + ":" + le.id + "@" + le.ts + "-" + re.id + "@" + re.ts);
                }
            }
        }
        return pairs;
    }

    private OfflineJoin() {
    }
}
