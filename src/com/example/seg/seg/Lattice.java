package com.example.seg.seg;

import com.example.seg.dict.Dictionary;

import java.util.ArrayList;
import java.util.List;

/**
 * 切分格：把输入文本展开为有向无环图的边。
 *
 * 边的语义是"占用 [start,end) 这一段字符"：
 * - 词典词边：从每个起点 i 用 Trie 枚举所有以 i 开始的词；
 * - 未知字边：每个字符单独成边（长度 1），代价取词典版本的 unknown-cost。
 *
 * 未知单元永远只按单字计价，因此多字未登录串 = 若干条未知字边，
 * 穷举与动态规划两种分词器共用这同一套边定义，保证两者可对照。
 */
public final class Lattice {

    /** 一条格边：覆盖 text[start,end)。 */
    public record Edge(int start, int end, String surface, boolean known, long cost) {
    }

    private final String text;
    /** 按边的终点 end 分组：incoming.get(end) 是所有到达 end 的边。 */
    private final List<List<Edge>> incoming;
    private final int dictMaxWordLength;

    private Lattice(String text, List<List<Edge>> incoming, int dictMaxWordLength) {
        this.text = text;
        this.incoming = incoming;
        this.dictMaxWordLength = dictMaxWordLength;
    }

    public static Lattice build(String text, Dictionary dict) {
        int n = text.length();
        List<List<Edge>> incoming = new ArrayList<>(n + 1);
        for (int i = 0; i <= n; i++) {
            incoming.add(new ArrayList<>());
        }

        for (int i = 0; i < n; i++) {
            final int start = i;
            // 1) 词典词边
            dict.trie().matchFrom(text, start, (len, scaledCost) -> {
                int end = start + len;
                incoming.get(end).add(new Edge(start, end, text.substring(start, end),
                        true, scaledCost));
            });
            // 2) 未知单字边（与恰好长度为 1 的词典词互斥：命中词典时不重复加未知边）
            String ch = text.substring(i, i + 1);
            if (dict.declaredCost(ch) == null) {
                incoming.get(i + 1).add(new Edge(i, i + 1, ch, false, dict.unknownCostScaled()));
            }
        }
        return new Lattice(text, incoming, Math.max(1, dict.maxWordLength()));
    }

    public String text() {
        return text;
    }

    public int length() {
        return text.length();
    }

    /** 所有到达顶点 end 的边。 */
    public List<Edge> incomingAt(int end) {
        return incoming.get(end);
    }

    /**
     * 所有从顶点 start 出发的边。Lattice 按终点分组存放，这里扫描
     * start 之后至多 maxWordLength 个顶点收集（仅小句穷举时使用）。
     */
    public List<Edge> outgoingFrom(int start) {
        List<Edge> result = new ArrayList<>();
        int limit = Math.min(text.length(), start + dictMaxWordLength);
        for (int end = start + 1; end <= limit; end++) {
            for (Edge edge : incoming.get(end)) {
                if (edge.start() == start) {
                    result.add(edge);
                }
            }
        }
        return result;
    }
}
