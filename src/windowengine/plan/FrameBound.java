package windowengine.plan;

/**
 * 帧边界：种类 + 位移。offset 仅对 PRECEDING / FOLLOWING 有效，
 * 其他种类构造时强制为 0。
 */
public record FrameBound(FrameBoundType type, long offset) {

    public FrameBound {
        if (type == FrameBoundType.PRECEDING || type == FrameBoundType.FOLLOWING) {
            if (offset < 0) {
                throw new windowengine.EngineException(windowengine.ErrorCode.INVALID_FRAME,
                        "帧位移不能为负: " + offset);
            }
        } else {
            offset = 0;
        }
    }

    /** 相对当前行位置的原始行号偏移（未做分区裁剪）。 */
    public long rawDelta() {
        return switch (type) {
            case UNBOUNDED_PRECEDING -> Long.MIN_VALUE;
            case PRECEDING -> -offset;
            case CURRENT_ROW -> 0;
            case FOLLOWING -> offset;
            case UNBOUNDED_FOLLOWING -> Long.MAX_VALUE;
        };
    }

    /** 边界在帧中的先后排序权重：越早的边界权重越小。 */
    public int positionOrder() {
        return switch (type) {
            case UNBOUNDED_PRECEDING -> 0;
            case PRECEDING -> 1;
            case CURRENT_ROW -> 2;
            case FOLLOWING -> 3;
            case UNBOUNDED_FOLLOWING -> 4;
        };
    }
}
