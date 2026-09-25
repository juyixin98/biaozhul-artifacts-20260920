package sessions.time;

/**
 * 可注入的"时间与调度"抽象。
 *
 * <p>生产代码只依赖事件时间（水位线）与事件时间定时器，不依赖任何墙上时钟，
 * 因此测试可以完全确定地驱动；如接入真实系统，可提供由源水印驱动的实现。
 *
 * <p>两类触发边界：
 * <ul>
 *   <li>{@link #registerTimer}（闭区间）：{@code W >= fireTime} 时触发。
 *       用于封窗——会话合并是 {@code 间隔 <= gap}，故水位线到达 {@code end+gap}
 *       这一刻即封存。</li>
 *   <li>{@link #registerTimerAfter}（严格大于）：{@code W > fireTime} 时触发。
 *       用于状态清除——窗口必须保留到"允许迟到的最后一个事件时刻"
 *       {@code end+gap+L} 结束，即只有水位线严格越过它才能清除。</li>
 * </ul>
 * 把水位线推进到 {@link Long#MAX_VALUE}（正无穷）时触发全部定时器，用于收尾。
 */
public interface TimerService {

    /** 当前水位线；尚未发出任何水位线时为 {@link Long#MIN_VALUE}。 */
    long currentWatermark();

    /**
     * 注册闭区间事件时间定时器：水位线 {@code W >= fireTime} 时触发。
     * 回调可能再次注册新的定时器，新定时器若已到点会在同一轮推进中继续触发。
     */
    void registerTimer(long fireTime, Runnable callback);

    /**
     * 注册严格大于事件时间定时器：水位线 {@code W > fireTime} 时触发。
     * 注意 {@code fireTime = Long.MAX_VALUE} 的定时器只能由正无穷水位线触发。
     */
    void registerTimerAfter(long fireTime, Runnable callback);

    /**
     * 把水位线推进到 {@code newWatermark}（单调不减；更小的值被忽略），
     * 并触发所有到点定时器（含回调中新注册的）。
     */
    void advanceWatermark(long newWatermark);
}
