package tvl.api;

/**
 * 把表达式错误的字符偏移量转换为行/列与可视化指示符。
 * 例如：
 * <pre>
 * a &gt; 1 AND
 *         ^
 * </pre>
 */
public final class ErrorFormatter {

    private ErrorFormatter() {
    }

    /** @return 长度为 3 的数组：[line(1-based), column(1-based), 带 ^ 指示符的两行片段] */
    public static int[] lineAndColumn(String source, int pos) {
        int line = 1;
        int column = 1;
        int safePos = Math.max(0, Math.min(pos, source.length()));
        for (int i = 0; i < safePos; i++) {
            if (source.charAt(i) == '\n') {
                line++;
                column = 1;
            } else {
                column++;
            }
        }
        return new int[]{line, column};
    }

    /** 生成 "源码行\n   ^" 形式的定位片段（只处理单行表达式的常见情形）。 */
    public static String caret(String source, int pos) {
        int safePos = Math.max(0, Math.min(pos, source.length()));
        int lineStart = source.lastIndexOf('\n', safePos - 1) + 1;
        int lineEnd = source.indexOf('\n', safePos);
        if (lineEnd < 0) lineEnd = source.length();
        String line = source.substring(lineStart, lineEnd);
        int offsetInLine = safePos - lineStart;
        return line + "\n" + " ".repeat(offsetInLine) + "^";
    }
}
