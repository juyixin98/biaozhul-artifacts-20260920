package com.example.edcand;

/**
 * Unicode 码点工具。
 *
 * <p>Java 的 {@code char} 是 UTF-16 代码单元；增补平面字符（如 emoji 😀）
 * 由两个 char（代理对）组成。本项目所有距离计算都以 <b>Unicode 码点</b>
 * 为单位，绝不把字节（UTF-8 字节数）或 UTF-16 代码单元当作字符距离。
 */
public final class CodePoints {

    private CodePoints() {
    }

    /** 把字符串展开成码点数组（每个元素是一个 Unicode scalar，不使用代理解析后的单元）。 */
    public static int[] of(String s) {
        int n = s.codePointCount(0, s.length());
        int[] out = new int[n];
        int idx = 0;
        for (int i = 0; i < s.length(); ) {
            int cp = s.codePointAt(i);
            out[idx++] = cp;
            i += Character.charCount(cp);
        }
        return out;
    }

    /** 码点数组长度（等价于字符数，不是字节数也不是 char 数）。 */
    public static int length(String s) {
        return s.codePointCount(0, s.length());
    }

    /** 码点数组重新拼回字符串。 */
    public static String toString(int[] cps) {
        StringBuilder sb = new StringBuilder(cps.length);
        for (int cp : cps) {
            sb.appendCodePoint(cp);
        }
        return sb.toString();
    }
}
