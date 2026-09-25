package dev.example.cp.engine;

/**
 * 检查点触发策略（可注入的调度）。
 *
 * <p>引擎每处理完一条事件就询问 {@link #shouldCheckpointAfterEvent}；
 * HTTP 服务还可以通过 {@code POST /checkpoints} 直接触发（屏障注入）。
 */
public interface CheckpointScheduler {

    /**
     * @param globalOffset 刚处理完的事件的<b>全局输入偏移</b>（从 0 起；跨恢复稳定，
     *                     不是本次进程内的计数），检查点边界应基于它决定
     * @param clock        可注入时钟
     * @return 是否应当在当前点插入屏障并完成一次检查点
     */
    boolean shouldCheckpointAfterEvent(long globalOffset, Clock clock);
}
