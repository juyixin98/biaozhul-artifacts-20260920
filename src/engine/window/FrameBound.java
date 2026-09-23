package engine.window;

/**
 * 帧边界。
 *
 * <p>{@code offset} 仅对 PRECEDING / FOLLOWING 有意义，且必须为非负整数；
 * 0 偏移视为 CURRENT ROW（解析层允许这种写法并归一化）。
 */
public record FrameBound(BoundKind kind, long offset) {

    public FrameBound {
        if (kind == BoundKind.PRECEDING || kind == BoundKind.FOLLOWING) {
            if (offset < 0) {
                throw new IllegalArgumentException("帧偏移量不能为负数: " + offset);
            }
        }
    }

    public static FrameBound unboundedPreceding() {
        return new FrameBound(BoundKind.UNBOUNDED_PRECEDING, 0);
    }

    public static FrameBound currentRow() {
        return new FrameBound(BoundKind.CURRENT_ROW, 0);
    }

    public static FrameBound unboundedFollowing() {
        return new FrameBound(BoundKind.UNBOUNDED_FOLLOWING, 0);
    }

    /**
     * 边界相对当前行的“位置序号”，用于判断 START / END 是否合法。
     * 无界端用 long 的极值表示。
     */
    public long relativePosition() {
        return switch (kind) {
            case UNBOUNDED_PRECEDING -> Long.MIN_VALUE;
            case PRECEDING -> -offset;
            case CURRENT_ROW -> 0;
            case FOLLOWING -> offset;
            case UNBOUNDED_FOLLOWING -> Long.MAX_VALUE;
        };
    }

    /** 边界落在分区中的物理下标；越界由调用方截断。采用饱和加减，杜绝大偏移回绕。 */
    public long absoluteIndex(long current) {
        return switch (kind) {
            case UNBOUNDED_PRECEDING -> Long.MIN_VALUE;
            case PRECEDING -> {
                long r = current - offset;
                yield r > current ? Long.MIN_VALUE : r; // 下溢饱和
            }
            case CURRENT_ROW -> current;
            case FOLLOWING -> {
                long r = current + offset;
                yield r < current ? Long.MAX_VALUE : r; // 上溢饱和
            }
            case UNBOUNDED_FOLLOWING -> Long.MAX_VALUE;
        };
    }

    public String toSql() {
        return switch (kind) {
            case UNBOUNDED_PRECEDING -> "UNBOUNDED PRECEDING";
            case PRECEDING -> offset + " PRECEDING";
            case CURRENT_ROW -> "CURRENT ROW";
            case FOLLOWING -> offset + " FOLLOWING";
            case UNBOUNDED_FOLLOWING -> "UNBOUNDED FOLLOWING";
        };
    }
}
