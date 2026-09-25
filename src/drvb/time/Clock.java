package drvb.time;

/**
 * 可注入时钟。
 *
 * <p>领域逻辑一律不得直接调用 {@link System#currentTimeMillis()}，而应经由本接口，
 * 这样测试可以用 {@link ManualClock} 精确控制处理时间。
 */
public interface Clock {
    /** 当前处理时间（毫秒）。 */
    long nowMillis();
}
