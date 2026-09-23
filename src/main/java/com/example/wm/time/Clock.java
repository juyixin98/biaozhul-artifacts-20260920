package com.example.wm.time;

/**
 * 可注入的时钟。事件流引擎只通过该接口感知“处理时间”，
 * 测试时使用 {@link VirtualClock}，生产环境使用 {@link SystemClock}。
 */
@FunctionalInterface
public interface Clock {
    long currentTimeMillis();

    static Clock system() {
        return new SystemClock();
    }
}
