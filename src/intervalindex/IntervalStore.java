package intervalindex;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.concurrent.locks.ReadWriteLock;
import java.util.concurrent.locks.ReentrantReadWriteLock;

/**
 * 区间多重集存储：用两棵增强 AVL 树支撑全部查询。
 *
 * <ul>
 *   <li><b>startTree</b>：键按 (lo, hi) 字典序排列，primary=lo，secondary=hi。
 *       子树最大 hi 用于交集查询的剪枝。</li>
 *   <li><b>endTree</b>：键按 (hi, lo) 字典序排列，primary=hi，secondary=lo。
 *       用于 O(log n) 统计 hi ≤ t 的区间份数。</li>
 * </ul>
 *
 * <p>覆盖计数（t 时刻）= #(lo ≤ t) − #(hi ≤ t)，分别由两棵树的
 * {@link AvlTree#countPrimaryLE(long)} 在 O(log n) 内给出。
 *
 * <p>所有公共方法线程安全（读写锁：查询并发，写操作独占）。
 */
public final class IntervalStore {

    /** start 树的键：(lo, hi)。 */
    record Key(long lo, long hi) implements Comparable<Key> {
        static final Comparator<Key> ORDER =
                Comparator.comparingLong(Key::lo).thenComparingLong(Key::hi);

        @Override
        public int compareTo(Key o) {
            return ORDER.compare(this, o);
        }
    }

    final AvlTree<Key> startTree =
            new AvlTree<>(Key.ORDER, Key::lo, Key::hi);
    final AvlTree<Key> endTree =
            new AvlTree<>(Comparator.comparingLong(Key::hi).thenComparingLong(Key::lo),
                    Key::hi, Key::lo);

    private final ReadWriteLock lock = new ReentrantReadWriteLock();

    /** 插入一份 [lo, hi)。返回该区间当前份数。空/逆序区间抛 IllegalArgumentException。 */
    public long insert(long lo, long hi) {
        Interval.of(lo, hi); // 校验
        Key key = new Key(lo, hi);
        lock.writeLock().lock();
        try {
            long after = startTree.insert(key);
            endTree.insert(key);
            return after;
        } finally {
            lock.writeLock().unlock();
        }
    }

    /**
     * 删除至多 amount 份 [lo, hi)。
     * @return 实际删除份数（0 表示不存在或 amount ≤ 0）
     */
    public long delete(long lo, long hi, long amount) {
        Interval.of(lo, hi); // 校验
        Key key = new Key(lo, hi);
        lock.writeLock().lock();
        try {
            long removed = startTree.erase(key, amount);
            if (removed > 0) {
                long removedEnd = endTree.erase(key, removed);
                if (removedEnd != removed) {
                    throw new AssertionError("internal inconsistency: endTree removed "
                            + removedEnd + " but startTree removed " + removed);
                }
            }
            return removed;
        } finally {
            lock.writeLock().unlock();
        }
    }

    /** 与 [lo, hi) 相交的全部区间（按 (lo,hi) 排序，含重复展开）。 */
    public List<Interval> queryOverlap(long lo, long hi) {
        Interval.of(lo, hi);
        lock.readLock().lock();
        try {
            List<Key> keys = startTree.collectOverlaps(lo, hi);
            List<Interval> result = new ArrayList<>();
            for (Key k : keys) {
                long count = startTree.getCount(k);
                for (long i = 0; i < count; i++) {
                    result.add(new Interval(k.lo(), k.hi()));
                }
            }
            return result;
        } finally {
            lock.readLock().unlock();
        }
    }

    /**
     * 时刻 t 的覆盖计数：满足 lo ≤ t &lt; hi 的区间份数。
     * = #(lo ≤ t) − #(hi ≤ t)。
     */
    public long coverage(long t) {
        lock.readLock().lock();
        try {
            return startTree.countPrimaryLE(t) - endTree.countPrimaryLE(t);
        } finally {
            lock.readLock().unlock();
        }
    }

    /** 当前不同区间数（重复只算 1）。 */
    public long distinctCount() {
        lock.readLock().lock();
        try {
            return startTree.distinctSize();
        } finally {
            lock.readLock().unlock();
        }
    }

    /** 当前区间总份数（含重复）。 */
    public long totalCount() {
        lock.readLock().lock();
        try {
            return startTree.totalCount();
        } finally {
            lock.readLock().unlock();
        }
    }

    /** 全部不同区间（按 (lo,hi) 排序）及各自份数。 */
    public List<Entry> all() {
        lock.readLock().lock();
        try {
            List<Key> keys = startTree.collectInOrder();
            List<Entry> out = new ArrayList<>(keys.size());
            for (Key k : keys) {
                out.add(new Entry(k.lo(), k.hi(), startTree.getCount(k)));
            }
            return out;
        } finally {
            lock.readLock().unlock();
        }
    }

    /** 一个不同区间及其份数。 */
    public record Entry(long lo, long hi, long count) {}
}
