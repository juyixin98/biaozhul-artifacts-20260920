package com.example.ac;

import java.util.Objects;

/**
 * 一条命中。
 *
 * <p>位置同时给出两套单位：{@code start/end} 是 Unicode code point 偏移（权威单位），
 * {@code charStart/charEnd} 是 Java UTF-16 单元偏移（辅助单位）。区间为左闭右开。
 *
 * <p>自然顺序（规范“事件序”）：先按结束位置，再按开始位置，最后按模式原始序号。
 * 流式分块按块顺序拼接的结果与整体匹配按此排序的结果完全一致；重复模式因 patternIndex
 * 不同而各自保留身份。
 */
public record Match(String patternId,
                    int start,
                    int end,
                    int charStart,
                    int charEnd,
                    int patternIndex,
                    String matched) implements Comparable<Match> {

    public Match {
        Objects.requireNonNull(patternId, "patternId");
        Objects.requireNonNull(matched, "matched");
    }

    public int length() {
        return end - start;
    }

    @Override
    public int compareTo(Match o) {
        int c = Integer.compare(end, o.end);
        if (c != 0) {
            return c;
        }
        c = Integer.compare(start, o.start);
        if (c != 0) {
            return c;
        }
        return Integer.compare(patternIndex, o.patternIndex);
    }
}
