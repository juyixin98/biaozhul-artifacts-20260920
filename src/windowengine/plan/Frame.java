package windowengine.plan;

import windowengine.EngineException;
import windowengine.ErrorCode;

/**
 * 滑动窗口帧：ROWS BETWEEN start AND end。
 * 构造时校验 SQL 合法性：起点不得晚于终点
 * （例如 ROWS BETWEEN 1 FOLLOWING AND 1 PRECEDING 是非法帧）。
 */
public record Frame(FrameMode mode, FrameBound start, FrameBound end) {

    public Frame {
        if (mode == null) {
            mode = FrameMode.ROWS;
        }
        if (start == null || end == null) {
            throw new EngineException(ErrorCode.INVALID_FRAME, "帧的起点和终点都必须指定");
        }
        if (!startBeforeOrEqualToEnd(start, end)) {
            throw new EngineException(ErrorCode.INVALID_FRAME,
                    "非法帧：起点晚于终点（" + describe(start) + " > " + describe(end) + "）");
        }
    }

    /**
     * 判断边界先后。同一种类直接比较 offset；
     * 不同种类按 UNBOUNDED PRECEDING &lt; PRECEDING &lt; CURRENT ROW
     * &lt; FOLLOWING &lt; UNBOUNDED FOLLOWING 的全序比较。
     */
    private static boolean startBeforeOrEqualToEnd(FrameBound a, FrameBound b) {
        if (a.positionOrder() != b.positionOrder()) {
            return a.positionOrder() < b.positionOrder();
        }
        return switch (a.type()) {
            // 同为 PRECEDING：offset 越大越早（3 PRECEDING 在 1 PRECEDING 之前）
            case PRECEDING -> a.offset() >= b.offset();
            // 同为 FOLLOWING：offset 越小越早
            case FOLLOWING -> a.offset() <= b.offset();
            // 无界/当前行：同种类即相同位置
            default -> true;
        };
    }

    static String describe(FrameBound bound) {
        return switch (bound.type()) {
            case UNBOUNDED_PRECEDING -> "UNBOUNDED PRECEDING";
            case PRECEDING -> bound.offset() + " PRECEDING";
            case CURRENT_ROW -> "CURRENT ROW";
            case FOLLOWING -> bound.offset() + " FOLLOWING";
            case UNBOUNDED_FOLLOWING -> "UNBOUNDED FOLLOWING";
        };
    }

    public String describe() {
        return "ROWS BETWEEN " + describe(start) + " AND " + describe(end);
    }

    // ---------- 常用预设帧 ----------

    /** ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW（SUM 默认帧）。 */
    public static Frame cumulativeDefault() {
        return new Frame(FrameMode.ROWS,
                new FrameBound(FrameBoundType.UNBOUNDED_PRECEDING, 0),
                new FrameBound(FrameBoundType.CURRENT_ROW, 0));
    }

    /** ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING（无 ORDER BY 时）。 */
    public static Frame wholePartition() {
        return new Frame(FrameMode.ROWS,
                new FrameBound(FrameBoundType.UNBOUNDED_PRECEDING, 0),
                new FrameBound(FrameBoundType.UNBOUNDED_FOLLOWING, 0));
    }
}
