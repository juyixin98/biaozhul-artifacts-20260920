package com.tjoin.core;

/**
 * 一侧缓冲事件数超过 {@link JoinConfig#maxBufferedPerSide()} 时抛出。
 *
 * <p>典型触发场景：某一侧水位线长期停滞（idle source），另一侧不断到达事件，
 * 停滞侧无法推动对侧状态清理，缓冲持续增长直到上限。
 */
public class BufferCapacityExceededException extends RuntimeException {

    private final StreamSide side;
    private final int capacity;

    public BufferCapacityExceededException(StreamSide side, int capacity) {
        super("Buffer for " + side + " stream exceeded capacity of " + capacity
                + " events (opposite-side watermark stalled?)");
        this.side = side;
        this.capacity = capacity;
    }

    public StreamSide side() {
        return side;
    }

    public int capacity() {
        return capacity;
    }
}
