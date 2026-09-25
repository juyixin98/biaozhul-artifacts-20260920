package streammatch.time;

/**
 * 可注入的一次性定时器调度器，供处理时间模式驱动 A 的窗口超时。
 *
 * <p>语义约定：
 * <ul>
 *   <li>{@code fireAt} 为“逻辑毫秒”。同一毫秒到期的任务，按注册先后依次执行（FIFO）。</li>
 *   <li>为消除“事件与超时恰在同一毫秒”的歧义，引擎注册超时时刻为
 *       {@code 到达时间 + windowMillis + 1}：即窗口 [tA, tA+W] 在 tA+W 时刻仍有效，
 *       到 tA+W+1 才超时。这与事件时间模式“watermark 严格大于 tA+W 才超时”保持一致。</li>
 * </ul>
 */
public interface TaskScheduler {

    /**
     * 在逻辑时间 {@code fireAtMillis}（含）之后的首次推进时执行 {@code task}。
     *
     * @return 不透明任务句柄，可用于 {@link #cancel(long)}
     */
    long scheduleAt(long fireAtMillis, Runnable task);

    /** 取消尚未触发的任务；若任务已触发或句柄未知则为空操作。 */
    void cancel(long handle);
}
