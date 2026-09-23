package engine.window;

/**
 * ROWS 物理窗口帧：[start, end]，端点均为相对当前行的物理偏移。
 *
 * <p>仅实现 ROWS 模式（不实现 RANGE / GROUPS）。帧在分区内越界时按
 * SQL 语义截断到分区边界，不会抛错；非法的帧定义（起点在终点之后）
 * 在解析阶段拒绝。
 */
public record Frame(FrameBound start, FrameBound end) {

    public Frame {
        if (start.relativePosition() > end.relativePosition()) {
            throw new IllegalArgumentException(
                    "非法窗口帧：起点 '" + start.toSql() + "' 不能位于终点 '" + end.toSql() + "' 之后");
        }
    }

    public static Frame defaultForSum() {
        return new Frame(FrameBound.unboundedPreceding(), FrameBound.currentRow());
    }

    public String toSql() {
        return "ROWS BETWEEN " + start.toSql() + " AND " + end.toSql();
    }
}
