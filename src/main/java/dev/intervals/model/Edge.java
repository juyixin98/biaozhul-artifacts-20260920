package dev.intervals.model;

/**
 * 端点包含性标志。
 */
public enum Edge {
    /** 开：端点不包含 */
    OPEN,
    /** 闭：端点包含 */
    CLOSED;

    public boolean included() {
        return this == CLOSED;
    }
}
