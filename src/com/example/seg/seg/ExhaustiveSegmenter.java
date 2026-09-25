package com.example.seg.seg;

import com.example.seg.dict.Dictionary;
import com.example.seg.model.SegPath;
import com.example.seg.model.Token;

import java.util.ArrayList;
import java.util.List;

/**
 * 穷举分词器：在与 {@link DpSegmenter} 完全相同的切分格上，用 DFS 枚举
 * 从顶点 0 到顶点 n 的 <b>所有</b> 路径，按统一决胜规则
 * （{@link SegPath#tieCompare}）排序后返回。
 *
 * 路径数量随句子长度指数增长，因此只用于小句子的对照验收，
 * 严禁用于长文本（超过 {@link #MAX_EXHAUSTIVE_CHARS} 直接拒绝）。
 */
public final class ExhaustiveSegmenter {

    /** 穷举允许的最大字符数。 */
    public static final int MAX_EXHAUSTIVE_CHARS = 16;

    /**
     * 返回全部切分路径，按 (cost, token数, 字典序) 从优到劣。
     */
    public List<SegPath> enumerate(String text, Dictionary dict) {
        if (text.length() > MAX_EXHAUSTIVE_CHARS) {
            throw new IllegalArgumentException(
                    "穷举只支持不超过 " + MAX_EXHAUSTIVE_CHARS + " 个字符的句子，实际长度 "
                            + text.length());
        }
        Lattice lattice = Lattice.build(text, dict);
        List<SegPath> all = new ArrayList<>();
        dfs(lattice, dict, 0, new ArrayList<>(), 0L, all);
        all.sort(SegPath::tieCompare);
        return all;
    }

    private void dfs(Lattice lattice, Dictionary dict, int pos,
                     List<Token> prefix, long prefixCost, List<SegPath> out) {
        int n = lattice.length();
        if (pos == n) {
            out.add(SegPath.of(prefix, prefixCost));
            return;
        }
        for (Lattice.Edge edge : lattice.outgoingFrom(pos)) {
            Token token = edge.known()
                    ? Token.known(edge.surface(), edge.cost(), dict.declaredCost(edge.surface()))
                    : Token.unknown(edge.surface(), edge.cost());
            prefix.add(token);
            dfs(lattice, dict, edge.end(), prefix, prefixCost + edge.cost(), out);
            prefix.remove(prefix.size() - 1);
        }
    }
}
