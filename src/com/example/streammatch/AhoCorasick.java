package com.example.streammatch;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * Aho-Corasick 多模式自动机（码点版）。
 *
 * <p>核心性质：</p>
 * <ul>
 *   <li>每个非空模式在 trie 中按字符串去重存储一次；同一字符串重复插入时
 *       每个重复模式都在输出列表中保留自己的独立模式 ID（见 {@link #outputs}）。</li>
 *   <li>失败链在 BFS 建自动机时完成；每条输出链路上的模式 ID 在插入时保持有序，
 *       因此任意状态产出的命中都按“更长的词在前”的顺序，最终由调用方统一按
 *       {@link Match#canonicalOrder()} 规范化排序。</li>
 *   <li>支持重叠匹配：同一位置可同时产出多个模式（he / she / his / hers 类经典情形）。</li>
 *   <li>{@link #go(int, int)} 带记忆化（on-demand），流式每个码点只需一次（摊还）转移。</li>
 * </ul>
 *
 * <p>本类本身是无状态的匹配器，可被多线程共享；流式状态由 {@link StreamMatcher} 持有。</p>
 */
public final class AhoCorasick {

    /** 单个 trie/自动机状态。 */
    static final class Node {
        /** goto 边：键为码点，值为状态下标；按需增长。 */
        int[] edgeCp = new int[0];
        int[] edgeTo = new int[0];
        /** 失败链接（根为 0）。 */
        int fail;
        /**
         * 本状态自身结束的模式 ID（插入顺序，自然有序）；
         * 完整输出 = 自身 + 失败链上各状态的自身输出。
         */
        int[] ownOutput = new int[0];
        /** BFS 构造完成后聚合好的完整输出（含失败链后缀），按 trie 深度降序。 */
        int[] outputs = new int[0];
    }

    final ArrayList<Node> nodes = new ArrayList<>();
    /** 按模式 ID 存放模式原文；含空模式（空串）。 */
    final ArrayList<String> patterns = new ArrayList<>();
    /** 空模式的 ID 列表（插入顺序）。 */
    final ArrayList<Integer> emptyIds = new ArrayList<>();
    final EmptyPatternPolicy emptyPolicy;

    /** 记忆化的完整 goto：键 = ((long) state << 32) | (cp & 0xffffffffL)。 */
    private final Long2IntOpenHashMap goCache = new Long2IntOpenHashMap();

    public AhoCorasick(List<String> patterns, EmptyPatternPolicy emptyPolicy) {
        this.emptyPolicy = emptyPolicy == null ? EmptyPatternPolicy.SKIP : emptyPolicy;
        this.nodes.add(new Node()); // 0 = 根
        for (String p : patterns) {
            addPattern(p == null ? "" : p);
        }
        build();
    }

    public AhoCorasick(List<String> patterns) {
        this(patterns, EmptyPatternPolicy.SKIP);
    }

    public int patternCount() {
        return patterns.size();
    }

    public EmptyPatternPolicy emptyPatternPolicy() {
        return emptyPolicy;
    }

    private void addPattern(String p) {
        int id = patterns.size();
        patterns.add(p);
        if (p.isEmpty()) {
            emptyIds.add(id);
            return;
        }
        int node = 0;
        int i = 0;
        while (i < p.length()) {
            int cp = p.codePointAt(i);
            int next = getEdge(node, cp);
            if (next < 0) {
                next = nodes.size();
                nodes.add(new Node());
                putEdge(node, cp, next);
            }
            node = next;
            i += Character.charCount(cp);
        }
        Node n = nodes.get(node);
        int len = n.ownOutput.length;
        n.ownOutput = Arrays.copyOf(n.ownOutput, len + 1);
        n.ownOutput[len] = id; // 插入顺序即 ID 升序
    }

    private int getEdge(int node, int cp) {
        Node n = nodes.get(node);
        for (int i = 0; i < n.edgeCp.length; i++) {
            if (n.edgeCp[i] == cp) {
                return n.edgeTo[i];
            }
        }
        return -1;
    }

    private void putEdge(int node, int cp, int to) {
        Node n = nodes.get(node);
        int len = n.edgeCp.length;
        n.edgeCp = Arrays.copyOf(n.edgeCp, len + 1);
        n.edgeTo = Arrays.copyOf(n.edgeTo, len + 1);
        n.edgeCp[len] = cp;
        n.edgeTo[len] = to;
    }

    /** BFS 计算失败链并聚合完整输出。 */
    private void build() {
        Node root = nodes.get(0);
        root.fail = 0;
        // outputs 已默认空；聚合时根的自身输出（不存在，空串不入 trie）也不影响
        root.outputs = root.ownOutput;

        int[] queue = new int[nodes.size()];
        int head = 0, tail = 0;
        for (int k = 0; k < root.edgeCp.length; k++) {
            int child = root.edgeTo[k];
            nodes.get(child).fail = 0;
            queue[tail++] = child;
        }
        while (head < tail) {
            int u = queue[head++];
            Node nu = nodes.get(u);
            Node nf = nodes.get(nu.fail);
            // 完整输出：先放自身（词更长），再接失败链聚合输出，保持深度降序。
            int[] fOut = nf.outputs;
            nu.outputs = concat(nu.ownOutput, fOut);
            for (int k = 0; k < nu.edgeCp.length; k++) {
                int cp = nu.edgeCp[k];
                int v = nu.edgeTo[k];
                nodes.get(v).fail = go(nu.fail, cp);
                queue[tail++] = v;
            }
        }
    }

    private static int[] concat(int[] a, int[] b) {
        if (b.length == 0) {
            return a;
        }
        int[] r = Arrays.copyOf(a, a.length + b.length);
        System.arraycopy(b, 0, r, a.length, b.length);
        return r;
    }

    /**
     * 完整 goto 转移：沿失败链回退直到找到边或回到根。带记忆化。
     *
     * @return 转移后的状态（找不到边时为根 0）
     */
    int go(int state, int cp) {
        long key = ((long) state << 32) | (cp & 0xffffffffL);
        int cached = goCache.getOrDefault(key, -1);
        if (cached != -1) {
            return cached;
        }
        int s = state;
        int result;
        int direct = getEdge(s, cp);
        if (direct >= 0) {
            result = direct;
        } else if (s == 0) {
            result = 0;
        } else {
            result = go(nodes.get(s).fail, cp); // 递归结果同样被记忆化
        }
        goCache.put(key, result);
        return result;
    }

    /** 状态 s 的完整输出模式 ID（深度降序，ID 仅在同深度/同长度级别内有序）。 */
    int[] outputsOf(int state) {
        return nodes.get(state).outputs;
    }

    String patternText(int id) {
        return patterns.get(id);
    }

    /**
     * 一次性匹配整段文本（便捷封装；内部用 {@link StreamMatcher} 保证与流式路径完全一致）。
     * 空文本在 SKIP 策略下返回空列表；BEFORE/AFTER 策略下返回位置 0 的空模式命中。
     */
    public List<Match> matchAll(String text) {
        StreamMatcher sm = new StreamMatcher(this);
        if (text != null && !text.isEmpty()) {
            sm.feedChars(text);
        }
        sm.finish();
        return new ArrayList<>(sm.allMatches);
    }

    /**
     * 将任意 {@link CharSequence} 展开为码点数组。
     * 非 BMP 字符（代理对）占一个码点；孤立代理按各自一个码点处理。
     */
    public static int[] toCodePoints(CharSequence s) {
        int n = s.length();
        int[] cps = new int[Character.codePointCount(s, 0, n)];
        int idx = 0;
        for (int i = 0; i < n; ) {
            int cp = Character.codePointAt(s, i);
            cps[idx++] = cp;
            i += Character.charCount(cp);
        }
        return cps;
    }
}
