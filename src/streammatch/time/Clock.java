package streammatch.time;

/**
 * 可注入时钟。生产用 {@link SystemClock}，测试与确定性处理时间计算用 {@link ManualClock}。
 */
public interface Clock {

    /** 当前时间（epoch 毫秒）。 */
    long now();
}
