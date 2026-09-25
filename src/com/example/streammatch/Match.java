package com.example.streammatch;

import java.util.Comparator;
import java.util.Objects;

/**
 * 一次模式命中。
 *
 * <p>所有位置均以<b> Unicode 码元点（code point）</b>为单位，相对于整个逻辑文本流的起点，
 * 与调用方如何分块、如何按 UTF-8 切字节完全无关。</p>
 *
 * @param patternId 模式 ID（插入编号；重复字符串模式各自保留独立 ID）
 * @param start     命中起点（含），码点偏移
 * @param end       命中终点（不含），码点偏移；空模式 start == end
 * @param pattern   命中的模式原文（便于响应中直接展示）
 */
public record Match(int patternId, int start, int end, String pattern) {

    public Match {
        Objects.requireNonNull(pattern, "pattern");
        if (start < 0 || end < start) {
            throw new IllegalArgumentException("非法位置: start=" + start + ", end=" + end);
        }
    }

    /** 规范化排序：起点、终点、模式 ID。朴素匹配与 AC 匹配按此顺序逐一比较。 */
    public static Comparator<Match> canonicalOrder() {
        return Comparator.comparingInt(Match::start)
                .thenComparingInt(Match::end)
                .thenComparingInt(Match::patternId);
    }
}
