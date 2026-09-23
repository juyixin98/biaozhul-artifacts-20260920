package dedup.core;

/**
 * 去重算子的最小接口。
 *
 * <p>处理基于事件时间：{@link #onWatermark(long)} 推进水位线并据此释放墓碑；
 * {@link #process(Event)} 必须在相应的水位线推进之后调用（由 StreamProcessor 保证顺序）。
 */
public interface Deduplicator {

    /** 处理一个事件，返回去重判定。 */
    DedupResult process(Event event);

    /**
     * 推进水位线（单调不减）。低于等于新水位线减 allowedLateness 的墓碑将被释放。
     * 回退（newWatermark &lt; 当前水位线，即“时钟回退”）被忽略并返回 false。
     *
     * @return true 表示水位线确实前进
     */
    boolean onWatermark(long newWatermark);

    /** 当前水位线；从未推进过时为 null。 */
    Long currentWatermark();

    /** 当前存活墓碑数（有界状态规模可观察）。 */
    int activeTombstones();

    /** 配置的允许乱序/迟到窗口（毫秒）。 */
    long allowedLateness();

    /** 墓碑硬上限。 */
    int maxTombstones();

    Stats stats();
}
