package com.example.quantiles.time;

/**
 * 可注入的时钟。事件时间语义的作业不依赖系统时间，测试中使用 {@link MockClock}，
 * 生产环境使用 {@link SystemClock}。
 */
public interface Clock {

    /** 当前时间（毫秒）。 */
    long nowMillis();
}
