package intervalindex;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Random;
import java.util.Set;
import java.util.TreeMap;

/**
 * 随机化属性测试（验收核心）：
 * 维护一份朴素多重集（ArrayList + TreeMap 计数）作为参照，
 * 在一系列随机插入/删除后，逐步对照：
 * <ol>
 *   <li>覆盖计数：随机时刻 t，树结构与暴力扫描一致；</li>
 *   <li>交集查询：随机 [lo,hi)，结果多重集与暴力扫描一致；</li>
 *   <li>枚举/计数：不同区间数、总份数一致；</li>
 *   <li>增强树内部不变量（BST 序、AVL 平衡、subtreeTotal、aug 等）始终成立。</li>
 * </ol>
 */
public class PropertyTest {

    private static final int OPS_PER_ROUND = 400;
    private static final int QUERIES_PER_CHECK = 60;
    private static final int COORD_BOUND = 40; // 小坐标域，制造大量嵌套/相邻/重复

    public static void run() {
        long seed = new Random().nextLong();
        System.out.println("PropertyTest seed = " + seed + " (固定种子复现：把该值传给 -Dseed=...)");
        String override = System.getProperty("seed");
        if (override != null && !override.isBlank()) {
            seed = Long.parseLong(override);
            System.out.println("PropertyTest using overridden seed = " + seed);
        }

        Random rnd = new Random(seed);
        for (int round = 0; round < 20; round++) {
            runRound(rnd, round);
        }

        // 专项：把所有区间逐个随机删光，每一步都对照
        runDrainScenario();
        System.out.println("PropertyTest OK");
    }

    private static void runRound(Random rnd, int round) {
        IntervalStore store = new IntervalStore();
        // 参照模型：键 -> 份数；另有展开多重集便于暴力
        TreeMap<IntervalStore.Key, Long> refCounts = new TreeMap<>(IntervalStore.Key.ORDER);
        List<int[]> refMultiset = new ArrayList<>();
        long total = 0;

        for (int op = 0; op < OPS_PER_ROUND; op++) {
            long lo = rnd.nextInt(COORD_BOUND) - COORD_BOUND / 2;
            long hi = lo + 1 + rnd.nextInt(COORD_BOUND); // 保证 lo < hi
            int action = rnd.nextInt(10);
            if (action < 7 || refMultiset.isEmpty()) {
                store.insert(lo, hi);
                refCounts.merge(new IntervalStore.Key(lo, hi), 1L, Long::sum);
                refMultiset.add(new int[]{(int) lo, (int) hi});
                total++;
            } else {
                long amount = rnd.nextInt(3) == 0 ? 1 + rnd.nextInt(4) : 1;
                long removed = store.delete(lo, hi, amount);
                long refCount = refCounts.getOrDefault(new IntervalStore.Key(lo, hi), 0L);
                long expectedRemoved = Math.min(amount, refCount);
                Check.eq(removed, expectedRemoved,
                        "round " + round + " op " + op + ": removed mismatch for [%d,%d)".formatted(lo, hi));
                for (long i = 0; i < removed; i++) {
                    boolean tookOne = removeFirst(refMultiset, (int) lo, (int) hi);
                    Check.that(tookOne, "reference multiset should contain deleted interval");
                }
                if (refCount - removed <= 0) {
                    refCounts.remove(new IntervalStore.Key(lo, hi));
                } else {
                    refCounts.put(new IntervalStore.Key(lo, hi), refCount - removed);
                }
                total -= removed;
            }

            if (op % 25 == 0 || op == OPS_PER_ROUND - 1) {
                checkInvariantsAndReference(store, refCounts, refMultiset, total, round, op, rnd);
            }
        }
    }

    private static boolean removeFirst(List<int[]> list, int lo, int hi) {
        for (int i = 0; i < list.size(); i++) {
            if (list.get(i)[0] == lo && list.get(i)[1] == hi) {
                list.remove(i);
                return true;
            }
        }
        return false;
    }

    private static void checkInvariantsAndReference(IntervalStore store,
                                                    TreeMap<IntervalStore.Key, Long> refCounts,
                                                    List<int[]> refMultiset,
                                                    long total,
                                                    int round, int op, Random rnd) {
        TreeInvariants.verifyBoth(store, total);
        Check.eq(store.totalCount(), total, "total mismatch r" + round + " op" + op);
        Check.eq(store.distinctCount(), refCounts.size(), "distinct mismatch r" + round + " op" + op);
        Check.eq((long) refMultiset.size(), total, "reference multiset size r" + round + " op" + op);

        // all() 与参照 TreeMap 完全一致
        List<IntervalStore.Entry> entries = store.all();
        Check.eq(entries.size(), refCounts.size(), "all() size r" + round + " op" + op);
        int idx = 0;
        for (var e : refCounts.entrySet()) {
            IntervalStore.Entry got = entries.get(idx++);
            Check.eq(got.lo(), e.getKey().lo(), "all() lo r" + round + " op" + op);
            Check.eq(got.hi(), e.getKey().hi(), "all() hi r" + round + " op" + op);
            Check.eq(got.count(), (long) e.getValue(), "all() count r" + round + " op" + op);
        }

        // 随机时刻覆盖计数 vs 暴力
        for (int q = 0; q < QUERIES_PER_CHECK; q++) {
            long t = rnd.nextInt(COORD_BOUND + 8) - COORD_BOUND / 2 - 4;
            long brute = 0;
            for (int[] iv : refMultiset) {
                if (iv[0] <= t && t < iv[1]) {
                    brute++;
                }
            }
            Check.eq(store.coverage(t), brute,
                    "coverage mismatch at t=" + t + " (r" + round + " op" + op + ")");
        }

        // 随机交集查询 vs 暴力（多重集比较）
        for (int q = 0; q < QUERIES_PER_CHECK; q++) {
            long lo = rnd.nextInt(COORD_BOUND + 4) - COORD_BOUND / 2 - 2;
            long hi = lo + 1 + rnd.nextInt(COORD_BOUND + 4);
            List<Interval> got = store.queryOverlap(lo, hi);
            // 暴力：收集并按 (lo,hi) 排序
            List<int[]> bruteHits = new ArrayList<>();
            for (int[] iv : refMultiset) {
                if (iv[0] < hi && iv[1] > lo) {
                    bruteHits.add(iv);
                }
            }
            bruteHits.sort((a, b) -> a[0] != b[0] ? Integer.compare(a[0], b[0])
                    : Integer.compare(a[1], b[1]));
            Check.eq(got.size(), bruteHits.size(),
                    "overlap size mismatch query [%d,%d) r%d op%d".formatted(lo, hi, round, op));
            for (int i = 0; i < got.size(); i++) {
                Check.eq(got.get(i).lo(), (long) bruteHits.get(i)[0], "overlap lo mismatch");
                Check.eq(got.get(i).hi(), (long) bruteHits.get(i)[1], "overlap hi mismatch");
            }
        }
    }

    /**
     * 专项场景：先插入一大批（含重复），再随机逐个删除，直到清空；
     * 每次删除后同时校验不变量、覆盖计数和若干交集查询。
     */
    private static void runDrainScenario() {
        Random rnd = new Random(987654321L);
        IntervalStore store = new IntervalStore();
        TreeMap<IntervalStore.Key, Long> ref = new TreeMap<>(IntervalStore.Key.ORDER);
        List<int[]> multiset = new ArrayList<>();

        Set<IntervalStore.Key> universe = new HashSet<>();
        for (int i = 0; i < 120; i++) {
            int lo = rnd.nextInt(30) - 15;
            int hi = lo + 1 + rnd.nextInt(30);
            IntervalStore.Key key = new IntervalStore.Key(lo, hi);
            universe.add(key);
            int copies = 1 + rnd.nextInt(3);
            for (int c = 0; c < copies; c++) {
                store.insert(lo, hi);
                multiset.add(new int[]{lo, hi});
            }
            ref.merge(key, (long) copies, Long::sum);
        }
        long total = multiset.size();
        TreeInvariants.verifyBoth(store, total);

        List<IntervalStore.Key> keys = new ArrayList<>(universe);
        while (!keys.isEmpty()) {
            IntervalStore.Key k = keys.remove(rnd.nextInt(keys.size()));
            long had = ref.get(k);
            long removed = store.delete(k.lo(), k.hi(), Long.MAX_VALUE);
            Check.eq(removed, had, "drain removed mismatch");
            for (long i = 0; i < had; i++) {
                Check.that(removeFirst(multiset, (int) k.lo(), (int) k.hi()),
                        "drain reference removal");
            }
            ref.remove(k);
            total -= had;
            TreeInvariants.verifyBoth(store, total);
            Check.eq(store.totalCount(), total, "drain total");
            Check.eq(store.distinctCount(), ref.size(), "drain distinct");

            // 每一步对若干固定时刻与固定查询做暴力对照
            long[] probes = {-15, -10, -5, -1, 0, 1, 5, 10, 15, 20, 30};
            for (long t : probes) {
                long brute = 0;
                for (int[] iv : multiset) {
                    if (iv[0] <= t && t < iv[1]) {
                        brute++;
                    }
                }
                Check.eq(store.coverage(t), brute, "drain coverage mismatch t=" + t);
            }
            long[][] qs = {{-20, 20}, {-15, -14}, {0, 1}, {-5, 6}, {10, 12}};
            for (long[] q : qs) {
                long brute = 0;
                for (int[] iv : multiset) {
                    if (iv[0] < q[1] && iv[1] > q[0]) {
                        brute++;
                    }
                }
                Check.eq((long) store.queryOverlap(q[0], q[1]).size(), brute,
                        "drain overlap mismatch [%d,%d)".formatted(q[0], q[1]));
            }
        }

        Check.eq(store.totalCount(), 0, "drained total zero");
        Check.eq(store.distinctCount(), 0, "drained distinct zero");
        Check.that(store.all().isEmpty(), "drained all() empty");
        TreeInvariants.verifyBoth(store, 0);
    }
}
