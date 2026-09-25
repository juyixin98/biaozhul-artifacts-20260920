package com.example.ac;

/** Unicode code point 工具。所有匹配位置均以 code point 偏移为准。 */
public final class CodePoints {

    private CodePoints() {
    }

    public static int[] of(String s) {
        return s.codePoints().toArray();
    }

    public static String asString(int[] codePoints, int offset, int length) {
        return new String(codePoints, offset, length);
    }
}
