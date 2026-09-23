package windowengine.plan;

/**
 * ROWS 帧边界种类。
 *   UNBOUNDED_PRECEDING / UNBOUNDED_FOLLOWING：分区两端，offset 无意义；
 *   PRECEDING / FOLLOWING：带非负整数 offset（语义上相对当前行的位移）；
 *   CURRENT_ROW：当前行。
 */
public enum FrameBoundType {
    UNBOUNDED_PRECEDING, PRECEDING, CURRENT_ROW, FOLLOWING, UNBOUNDED_FOLLOWING
}
