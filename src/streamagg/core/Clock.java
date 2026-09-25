package streamagg.core;

/**
 * 可注入的时间源。
 * 生产环境使用 {@link SystemClock}（真实墙钟时间）；测试使用 {@link ManualClock}
 * （由测试手动推进时间），保证结果可复现。
 */
public interface Clock {
    long nowMillis();
}
