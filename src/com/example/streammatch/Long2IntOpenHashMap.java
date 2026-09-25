package com.example.streammatch;

/**
 * 极简 long→int 开放寻址哈希表（自动机 goto 转移记忆化用）。
 * 值 0 是合法状态，因此用“键是否存在”而不是哨兵值判断命中。
 */
final class Long2IntOpenHashMap {
    private long[] keys;
    private int[] values;
    private boolean[] used;
    private int size;
    private int mask;

    Long2IntOpenHashMap() {
        int cap = 16;
        keys = new long[cap];
        values = new int[cap];
        used = new boolean[cap];
        mask = cap - 1;
    }

    int size() {
        return size;
    }

    int getOrDefault(long key, int def) {
        int i = mix(key) & mask;
        while (used[i]) {
            if (keys[i] == key) {
                return values[i];
            }
            i = (i + 1) & mask;
        }
        return def;
    }

    void put(long key, int value) {
        if (size * 2 >= keys.length) {
            rehash();
        }
        int i = mix(key) & mask;
        while (used[i]) {
            if (keys[i] == key) {
                values[i] = value;
                return;
            }
            i = (i + 1) & mask;
        }
        used[i] = true;
        keys[i] = key;
        values[i] = value;
        size++;
    }

    private void rehash() {
        long[] oldKeys = keys;
        int[] oldValues = values;
        boolean[] oldUsed = used;
        int cap = keys.length * 2;
        keys = new long[cap];
        values = new int[cap];
        used = new boolean[cap];
        mask = cap - 1;
        size = 0;
        for (int j = 0; j < oldUsed.length; j++) {
            if (oldUsed[j]) {
                put(oldKeys[j], oldValues[j]);
            }
        }
    }

    private static int mix(long x) {
        // SplitMix64 finalizer —— 对顺序性强的 state/cp 组合键打散效果好
        x += 0x9e3779b97f4a7c15L;
        x = (x ^ (x >>> 30)) * 0xbf58476d1ce4e5b9L;
        x = (x ^ (x >>> 27)) * 0x94d049bb133111ebL;
        return (int) (x ^ (x >>> 31));
    }
}
