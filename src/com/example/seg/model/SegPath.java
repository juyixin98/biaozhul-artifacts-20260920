package com.example.seg.model;

import java.util.List;

/**
 * 一条完整的切分路径。
 *
 * @param tokens 切分单元序列，首尾相接恰好覆盖输入原文
 * @param cost   全路径放大整数总代价（各 token 代价之和）
 */
public record SegPath(List<Token> tokens, long cost) {

    public static SegPath of(List<Token> tokens, long cost) {
        return new SegPath(List.copyOf(tokens), cost);
    }

    /**
     * 确定性同分时的决胜规则（与动态规划剪枝所用规则完全一致）：
     * 1) 总代价小者优先（调用方一般已先按 cost 排序，这里仍比较一次，保证自包含）；
     * 2) 代价相同，切分单元个数少者优先（更"整"的切分）；
     * 3) 仍相同，按 token 字面序列的字典序（Unicode code point 序）更小者优先。
     *
     * @return 负数表示 a 排在 b 前面
     */
    public static int tieCompare(SegPath a, SegPath b) {
        int cmp = Long.compare(a.cost, b.cost);
        if (cmp != 0) {
            return cmp;
        }
        cmp = Integer.compare(a.tokens.size(), b.tokens.size());
        if (cmp != 0) {
            return cmp;
        }
        for (int i = 0; i < a.tokens.size(); i++) {
            cmp = a.tokens.get(i).surface().compareTo(b.tokens.get(i).surface());
            if (cmp != 0) {
                return cmp;
            }
        }
        return 0;
    }

    /** 用 / 连接的可读形式，例如 "研究/生命"。 */
    public String joined() {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < tokens.size(); i++) {
            if (i > 0) {
                sb.append('/');
            }
            sb.append(tokens.get(i).surface());
        }
        return sb.toString();
    }
}
