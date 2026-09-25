package com.example.ac;

import java.util.Objects;

/**
 * 一个待匹配模式。{@code index} 是模式在编译输入中的原始序号（包含空模式在内），
 * 重复模式各自拥有独立的 index / id 身份。
 */
public record Pattern(String id, String literal, int[] codePoints, int index) {

    public Pattern {
        Objects.requireNonNull(id, "id");
        Objects.requireNonNull(literal, "literal");
        Objects.requireNonNull(codePoints, "codePoints");
    }

    public static Pattern of(String id, String literal) {
        return new Pattern(id, literal, CodePoints.of(literal), -1);
    }

    public Pattern withIndex(int newIndex) {
        return new Pattern(id, literal, codePoints, newIndex);
    }

    public boolean isEmpty() {
        return codePoints.length == 0;
    }

    /** UTF-16 单元长度（String#length 语义），用于报告 charStart/charEnd。 */
    public int utf16Length() {
        return literal.length();
    }
}
