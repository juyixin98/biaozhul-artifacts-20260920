package com.example.ac;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * 朴素多模式匹配（暴力），仅用于测试中作为独立参考实现（oracle）。
 * 与 {@link StreamingMatcher} 完全独立：直接做子串包含检查，
 * 再按与 AC 相同的规范事件序排序。
 */
public final class NaiveMatcher {

    private NaiveMatcher() {
    }

    public static List<Match> match(List<Pattern> input, EmptyPatternPolicy policy, String text) {
        if (policy == EmptyPatternPolicy.ERROR) {
            for (Pattern p : input) {
                if (p.isEmpty()) {
                    throw new IllegalArgumentException("empty pattern present under ERROR policy");
                }
            }
        }
        int[] cps = CodePoints.of(text);
        List<Match> out = new ArrayList<>();
        for (int idx = 0; idx < input.size(); idx++) {
            Pattern p = input.get(idx).withIndex(idx);
            if (p.isEmpty()) {
                if (policy == EmptyPatternPolicy.MATCH_EVERY_POSITION) {
                    int[] charOffset = charOffsets(cps);
                    for (int pos = 0; pos <= cps.length; pos++) {
                        out.add(new Match(p.id(), pos, pos, charOffset[pos], charOffset[pos], idx, ""));
                    }
                }
                continue;
            }
            int[] pat = p.codePoints();
            for (int start = 0; start + pat.length <= cps.length; start++) {
                boolean ok = true;
                for (int k = 0; k < pat.length; k++) {
                    if (cps[start + k] != pat[k]) {
                        ok = false;
                        break;
                    }
                }
                if (ok) {
                    int charStart = charOffset(cps, start);
                    int charEnd = charOffset(cps, start + pat.length);
                    out.add(new Match(p.id(), start, start + pat.length,
                            charStart, charEnd, idx, p.literal()));
                }
            }
        }
        out.sort(Comparator.naturalOrder());
        return out;
    }

    /** code point 偏移 -> UTF-16 单元偏移。 */
    private static int charOffset(int[] cps, int cpOffset) {
        int chars = 0;
        for (int i = 0; i < cpOffset; i++) {
            chars += Character.charCount(cps[i]);
        }
        return chars;
    }

    private static int[] charOffsets(int[] cps) {
        int[] off = new int[cps.length + 1];
        int chars = 0;
        for (int i = 0; i < cps.length; i++) {
            off[i] = chars;
            chars += Character.charCount(cps[i]);
        }
        off[cps.length] = chars;
        return off;
    }
}
