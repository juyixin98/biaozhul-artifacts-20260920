package com.winquant;

/**
 * 可注入的时间源。生产环境使用 {@link SystemClock}，测试使用 {@link ManualClock}。
 * 时间单位由使用方决定（本库统一使用毫秒，但任何单调单位均可）。
 */
public interface Clock {

    /** 当前时间，必须单调不减。 */
    long now();
}
