package engine.window;

/**
 * 一个窗口函数的计算规格。
 *
 * <ul>
 *   <li>{@code function}：ROW_NUMBER / RANK / SUM；</li>
 *   <li>{@code outputColumn}：结果列名，必须与输入列名不重复；</li>
 *   <li>{@code argumentColumn}：仅 SUM 需要，必须是 LONG 列；</li>
 *   <li>{@code frame}：仅 SUM 使用；ROW_NUMBER / RANK 忽略帧。</li>
 * </ul>
 */
public record FunctionSpec(
        WindowFunction function,
        String outputColumn,
        String argumentColumn,
        Frame frame) {

    public static FunctionSpec rowNumber(String outputColumn) {
        return new FunctionSpec(WindowFunction.ROW_NUMBER, outputColumn, null, null);
    }

    public static FunctionSpec rank(String outputColumn) {
        return new FunctionSpec(WindowFunction.RANK, outputColumn, null, null);
    }

    public static FunctionSpec sum(String outputColumn, String argumentColumn, Frame frame) {
        return new FunctionSpec(WindowFunction.SUM, outputColumn, argumentColumn, frame);
    }

    public String toSql() {
        return switch (function) {
            case ROW_NUMBER -> "ROW_NUMBER()";
            case RANK -> "RANK()";
            case SUM -> "SUM(" + argumentColumn + ") OVER " + frame.toSql();
        };
    }
}
