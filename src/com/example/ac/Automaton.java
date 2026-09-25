package com.example.ac;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Aho-Corasick 自动机（Trie + failure link + dictionary link）。
 *
 * <p>基于 code point 转移，支持 Unicode 补充平面字符（emoji 等代理对）。
 * 重复模式在 Trie 上共享路径，但每个终止节点保存全部 patternIndex，
 * 因此每个重复模式都保留独立身份。
 *
 * <p>搜索时沿 dictionary link 枚举当前状态的全部后缀输出，正确处理“同一结束位置
 * 同时命中多个不同长度模式”的重叠匹配。
 */
final class Automaton {

    static final class Node {
        final Map<Integer, Integer> gotoMap = new HashMap<>();
        int fail;
        int dictLink = -1;
        /** 终止于此节点的模式原始序号（可含多个重复模式，按序号升序）。 */
        int[] terminals = new int[0];
    }

    final List<Node> nodes = new ArrayList<>();

    Automaton() {
        nodes.add(new Node()); // 根节点
    }

    int newNode() {
        nodes.add(new Node());
        return nodes.size() - 1;
    }

    /** 将一个非空模式插入 Trie，并把 patternIndex 追加到终止节点。 */
    void insert(int[] cps, int patternIndex) {
        int state = 0;
        for (int cp : cps) {
            state = nodes.get(state).gotoMap.computeIfAbsent(cp, k -> newNode());
        }
        Node end = nodes.get(state);
        int[] t = end.terminals;
        int[] grown = new int[t.length + 1];
        System.arraycopy(t, 0, grown, 0, t.length);
        grown[t.length] = patternIndex; // 插入顺序即序号升序
        end.terminals = grown;
    }

    /** BFS 计算 failure link 与 dictionary link。 */
    void build() {
        ArrayDeque<Integer> queue = new ArrayDeque<>();
        for (int child : nodes.get(0).gotoMap.values()) {
            nodes.get(child).fail = 0;
            queue.add(child);
        }
        while (!queue.isEmpty()) {
            int r = queue.poll();
            Node rn = nodes.get(r);
            for (Map.Entry<Integer, Integer> e : rn.gotoMap.entrySet()) {
                int cp = e.getKey();
                int s = e.getValue();
                int f = rn.fail;
                while (f != 0 && !nodes.get(f).gotoMap.containsKey(cp)) {
                    f = nodes.get(f).fail;
                }
                Integer step = nodes.get(f).gotoMap.get(cp);
                nodes.get(s).fail = (step != null && step != s) ? step : 0;
                int failState = nodes.get(s).fail;
                nodes.get(s).dictLink =
                        nodes.get(failState).terminals.length > 0 ? failState : nodes.get(failState).dictLink;
                queue.add(s);
            }
        }
        goCache.clear();
    }

    /** 稀疏转移缓存：goto 边优先，否则沿 failure link 回退，根节点回退到自身。 */
    private final Map<Long, Integer> goCache = new HashMap<>();

    int go(int state, int cp) {
        long key = ((long) state << 21) | (cp & 0x1FFFFFL);
        Integer cached = goCache.get(key);
        if (cached != null) {
            return cached;
        }
        int result;
        Integer direct = nodes.get(state).gotoMap.get(cp);
        if (direct != null) {
            result = direct;
        } else if (state == 0) {
            result = 0;
        } else {
            result = go(nodes.get(state).fail, cp);
        }
        goCache.put(key, result);
        return result;
    }
}
