package intervaljoin;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;

/**
 * 离线全量区间连接（独立于流式引擎的参照实现）。
 *
 * 不使用任何水位概念：把两侧全部事件收下，按 key 分组、按时间排序后二分窗口。
 * 流计算结果（排除迟到丢弃的事件后）应与本实现的输出集合完全一致。
 */
public final class OfflineJoin {

    private OfflineJoin() {}

    /** 嵌套循环版本 O(n*m)，用于小数据交叉验证二分版本本身的正确性。 */
    public static List<Pair> nestedLoop(List<Event> events, long lower, long upper) {
        List<Event> left = new ArrayList<>();
        List<Event> right = new ArrayList<>();
        for (Event e : events) {
            if ("left".equals(e.side)) left.add(e);
            else right.add(e);
        }
        List<Pair> out = new ArrayList<>();
        for (Event l : left) {
            for (Event r : right) {
                long d = r.ts - l.ts;
                if (l.key.equals(r.key) && d >= lower && d <= upper) {
                    out.add(new Pair(l, r));
                }
            }
        }
        return out;
    }

    /** 排序 + 二分版本 O((n+m) log n)，验收大数据量时使用。 */
    public static List<Pair> sortMerge(List<Event> events, long lower, long upper) {
        List<Event> left = new ArrayList<>();
        List<Event> right = new ArrayList<>();
        for (Event e : events) {
            if ("left".equals(e.side)) left.add(e);
            else right.add(e);
        }
        // 按 (key, ts) 排序，同一 key 内时间有序，可二分窗口。
        Comparator<Event> cmp = Comparator.comparing((Event e) -> e.key).thenComparingLong(e -> e.ts);
        left.sort(cmp);
        right.sort(cmp);

        List<Pair> out = new ArrayList<>();
        int i = 0;
        while (i < left.size()) {
            int j = i;
            while (j < left.size() && left.get(j).key.equals(left.get(i).key)) j++;
            List<Event> lBucket = left.subList(i, j);          // 已按 ts 升序
            List<Event> rBucket = bucketOf(right, left.get(i).key);
            for (Event l : lBucket) {
                long lo = l.ts + lower;
                long hi = l.ts + upper;
                int from = lowerBound(rBucket, lo);
                int to = upperBound(rBucket, hi);
                for (int k = from; k < to; k++) {
                    out.add(new Pair(l, rBucket.get(k)));
                }
            }
            i = j;
        }
        return out;
    }

    private static List<Event> bucketOf(List<Event> sorted, String key) {
        int from = lowerBoundKey(sorted, key);
        int to = upperBoundKey(sorted, key);
        return from == to ? Collections.emptyList() : sorted.subList(from, to);
    }

    /** 首个 ts >= target 的下标（rBucket 按 ts 升序）。 */
    private static int lowerBound(List<Event> a, long target) {
        int lo = 0, hi = a.size();
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (a.get(mid).ts < target) lo = mid + 1;
            else hi = mid;
        }
        return lo;
    }

    /** 首个 ts > target 的下标。 */
    private static int upperBound(List<Event> a, long target) {
        int lo = 0, hi = a.size();
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (a.get(mid).ts <= target) lo = mid + 1;
            else hi = mid;
        }
        return lo;
    }

    private static int lowerBoundKey(List<Event> a, String key) {
        int lo = 0, hi = a.size();
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (a.get(mid).key.compareTo(key) < 0) lo = mid + 1;
            else hi = mid;
        }
        return lo;
    }

    private static int upperBoundKey(List<Event> a, String key) {
        int lo = 0, hi = a.size();
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (a.get(mid).key.compareTo(key) <= 0) lo = mid + 1;
            else hi = mid;
        }
        return lo;
    }
}
