package intervaljoin;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Random;
import java.util.Set;

/**
 * 确定性测试数据生成：一个极端热键 + 大量冷键。
 *
 * 热键：左右各 hotN 条，时间戳在 [1, hotN] 密集分布（含同刻），
 *       形成 O(hotN) 量级的配对，验证热键正确性与回收。
 * 冷键：coldKeys 个键，每键左右各一条，时间戳精心布置，
 *       恰好产生 1 个配对/键（命中下界或上界），覆盖边界。
 */
public final class DataGen {

    public static final String HOT_KEY = "hot";

    private DataGen() {}

    public static final class Dataset {
        public final List<Event> events = new ArrayList<>();
        public final int hotN;
        public final int coldKeys;

        Dataset(int hotN, int coldKeys) {
            this.hotN = hotN;
            this.coldKeys = coldKeys;
        }
    }

    /**
     * @param hotN    热键每侧事件数
     * @param coldKeys 冷键个数
     * @param lower   区间下界（含）
     * @param upper   区间上界（含）
     * @param seed    随机种子（控制交错顺序）
     */
    public static Dataset generate(int hotN, int coldKeys, long lower, long upper, long seed) {
        Dataset ds = new Dataset(hotN, coldKeys);
        Random rnd = new Random(seed);

        // 热键：时间戳 1..hotN，两侧完全同刻；为制造同刻，右流时间戳循环到更少取值
        for (int t = 1; t <= hotN; t++) {
            ds.events.add(new Event("left", HOT_KEY, t, "L-hot-" + t));
            int rt = 1 + Math.floorMod(t * 7 + 3, hotN); // 伪随机但确定的时间戳
            ds.events.add(new Event("right", HOT_KEY, rt, "R-hot-" + t));
        }

        // 冷键：左事件时间 = base，右事件时间 = base + delta，
        // delta 交替取 lower / upper / 中间值 / 区间外，保证边界命中与不命中都覆盖。
        long base = hotN + 100L;
        for (int k = 0; k < coldKeys; k++) {
            String key = "cold-" + k;
            long lt = base + k * 10L;
            long delta;
            switch (k % 4) {
                case 0: delta = lower;            // 恰好命中下界
                    break;
                case 1: delta = upper;            // 恰好命中上界
                    break;
                case 2: delta = (lower + upper) / 2; // 区间内
                    break;
                default: delta = upper + 1 + (k % 3); // 区间外（含上界+1 边界）
                    break;
            }
            // lower 可能为负，右事件时间仍需为正，base 已留足偏移
            ds.events.add(new Event("left", key, lt, "L-" + key));
            ds.events.add(new Event("right", key, lt + delta, "R-" + key));
        }

        // 打乱到达顺序（确定性）
        java.util.Collections.shuffle(ds.events, rnd);
        return ds;
    }

    /** 把 Pair 列表转成集合，用于无序集合比对。元素为 "leftId|rightId"。 */
    public static Set<String> toIdSet(List<Pair> pairs) {
        Set<String> set = new HashSet<>(pairs.size() * 2);
        for (Pair p : pairs) {
            set.add(p.left.id + "|" + p.right.id);
        }
        return set;
    }
}
