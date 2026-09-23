package com.example.paginate.store;

import com.example.paginate.model.Item;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.locks.ReentrantLock;

/**
 * 内存数据存储（当前真实状态）。
 *
 * 所有修改加锁；快照通过 {@link #snapshotItems()} 拷贝不可变列表获得，
 * 快照与后续写入互不影响（Item 的修改在更新时采用整体替换的方式，见 update）。
 */
public final class ItemStore {

    private final List<Item> items = new ArrayList<>();
    private final AtomicLong idSequence;
    private final ReentrantLock lock = new ReentrantLock();

    public ItemStore() {
        this(seedDefault());
    }

    /** 测试用：可注入初始数据集。 */
    public ItemStore(List<Item> seed) {
        items.addAll(seed);
        long max = 0;
        for (Item it : seed) {
            max = Math.max(max, it.id());
        }
        this.idSequence = new AtomicLong(Math.max(max, 1000));
    }

    public static List<Item> seedDefault() {
        String[] categories = {"books", "music", "movies"};
        // 故意制造大量重名/同分：name 在三个分类间重复，score 也有重复值
        String[] names = {
                "alpha", "beta", "gamma", "delta", "echo", "foxtrot", "golf", "hotel",
                "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
                "quebec", "romeo", "sierra"
        };
        List<Item> seed = new ArrayList<>();
        long id = 1;
        for (int round = 0; round < 3; round++) {
            for (int i = 0; i < names.length; i++) {
                // score 在 10、20、30 等少数字段上重复，制造排序键重复
                long score = ((i + round * 5) % 6) * 10L;
                seed.add(new Item(id++, names[i], categories[round], score));
            }
        }
        return seed;
    }

    /** 返回当前数据的不可变快照拷贝。 */
    public List<Item> snapshotItems() {
        lock.lock();
        try {
            return List.copyOf(items);
        } finally {
            lock.unlock();
        }
    }

    public Item create(Long requestedId, String name, String category, long score) {
        lock.lock();
        try {
            long id;
            if (requestedId != null) {
                long requested = requestedId;
                if (findIndex(requested) >= 0) {
                    return null; // id 冲突
                }
                idSequence.updateAndGet(cur -> Math.max(cur, requested));
                id = requested;
            } else {
                id = idSequence.incrementAndGet();
                while (findIndex(id) >= 0) {
                    id = idSequence.incrementAndGet();
                }
            }
            Item item = new Item(id, name, category, score);
            items.add(item);
            return item;
        } finally {
            lock.unlock();
        }
    }

    public Optional<Item> find(long id) {
        lock.lock();
        try {
            int idx = findIndex(id);
            return idx < 0 ? Optional.empty() : Optional.of(items.get(idx));
        } finally {
            lock.unlock();
        }
    }

    /** 整体替换：快照里持有的旧 Item 对象不会被原地修改，保证快照不可变。 */
    public Optional<Item> update(long id, String name, String category, Long score) {
        lock.lock();
        try {
            int idx = findIndex(id);
            if (idx < 0) {
                return Optional.empty();
            }
            Item old = items.get(idx);
            Item updated = new Item(
                    old.id(),
                    name != null ? name : old.name(),
                    category != null ? category : old.category(),
                    score != null ? score : old.score());
            items.set(idx, updated);
            return Optional.of(updated);
        } finally {
            lock.unlock();
        }
    }

    public boolean delete(long id) {
        lock.lock();
        try {
            int idx = findIndex(id);
            if (idx < 0) {
                return false;
            }
            items.remove(idx);
            return true;
        } finally {
            lock.unlock();
        }
    }

    public int size() {
        lock.lock();
        try {
            return items.size();
        } finally {
            lock.unlock();
        }
    }

    public List<Long> allIds() {
        lock.lock();
        try {
            List<Long> ids = new ArrayList<>(items.size());
            for (Item it : items) {
                ids.add(it.id());
            }
            return ids;
        } finally {
            lock.unlock();
        }
    }

    public Set<Long> idSet() {
        lock.lock();
        try {
            Set<Long> ids = new HashSet<>();
            for (Item it : items) {
                ids.add(it.id());
            }
            return ids;
        } finally {
            lock.unlock();
        }
    }

    private int findIndex(long id) {
        for (int i = 0; i < items.size(); i++) {
            if (items.get(i).id() == id) {
                return i;
            }
        }
        return -1;
    }
}
