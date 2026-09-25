package com.example.streammatch;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * 朴素多模式匹配（参照实现 / oracle）。
 *
 * <p>用最直接的 O(n·m) 枚举实现，刻意不使用任何 KMP/AC 技巧，以便在测试中
 * 与 {@link AhoCorasick} 的结果做独立交叉验证。</p>
 *
 * <p>语义与 AC 侧严格一致：</p>
 * <ul>
 *   <li>位置以码点为单位；</li>
 *   <li>每个重复模式独立产出（重复字符串各算一次，ID 各自保留）；</li>
 *   <li>允许重叠（同一起点/终点多个模式，以及不同起点的重叠命中）；</li>
 *   <li>空模式在 p ∈ [0, n] 的每个间隙命中；策略决定的是产出时机而非集合，
 *       因此整流参照时 BEFORE/AFTER 产出的集合相同（同一位置上按 ID 排序）。</li>
 * </ul>
 */
public final class NaiveMatcher {

    private NaiveMatcher() {
    }

    /** 对整段文本做朴素匹配，结果按 {@link Match#canonicalOrder()} 规范化排序。 */
    public static List<Match> match(String text, List<String> patterns, EmptyPatternPolicy policy) {
        int[] cps = AhoCorasick.toCodePoints(text == null ? "" : text);
        List<Match> out = new ArrayList<>();
        for (int id = 0; id < patterns.size(); id++) {
            String p = patterns.get(id);
            if (p.isEmpty()) {
                if (policy == EmptyPatternPolicy.SKIP) {
                    continue;
                }
                for (int pos = 0; pos <= cps.length; pos++) {
                    out.add(new Match(id, pos, pos, ""));
                }
                continue;
            }
            int[] pat = AhoCorasick.toCodePoints(p);
            for (int start = 0; start + pat.length <= cps.length; start++) {
                boolean eq = true;
                for (int k = 0; k < pat.length; k++) {
                    if (cps[start + k] != pat[k]) {
                        eq = false;
                        break;
                    }
                }
                if (eq) {
                    out.add(new Match(id, start, start + pat.length, p));
                }
            }
        }
        out.sort(Comparator.comparingInt(Match::start)
                .thenComparingInt(Match::end)
                .thenComparingInt(Match::patternId));
        return out;
    }
}
