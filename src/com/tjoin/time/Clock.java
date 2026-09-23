package com.tjoin.time;

/**
 * 可注入的时钟。业务逻辑依赖此抽象而非 {@code System.currentTimeMillis()}，
 * 使时间相关行为在测试中完全确定、可回放。
 */
public interface Clock {

    /** 当前处理时间（毫秒）。 */
    long now();

    /** 系统墙钟时钟。 */
    static Clock system() {
        return SystemClock.INSTANCE;
    }

    /** 从给定起点开始、可手动推进的测试时钟。 */
    static Clock manual(long startMillis) {
        return new ManualClock(startMillis);
    }
}
