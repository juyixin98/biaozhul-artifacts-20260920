package engine.window;

/** 帧边界种类（仅支持 ROWS 模式的物理行偏移）。 */
public enum BoundKind {
    UNBOUNDED_PRECEDING,
    PRECEDING,
    CURRENT_ROW,
    FOLLOWING,
    UNBOUNDED_FOLLOWING
}
