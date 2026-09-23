package dedup.watermark;

import dedup.core.Event;

/**
 * 水位线生成器：根据事件流/时间产生水位线，交给去重算子推进。
 * 水位线 t 的含义：“事件时间 &lt; t 的数据基本到齐了”。
 */
public interface WatermarkGenerator {

    /**
     * 每来一个事件调用。
     *
     * @return 新水位线（若没有前进则返回 null）
     */
    Long onEvent(Event event);

    /**
     * 周期性触发（处理时间时钟）。
     *
     * @return 新水位线（若没有前进则返回 null）
     */
    Long onPeriodicTick(long currentClockMillis);

    /** 当前水位线（从未产生时为 null）。 */
    Long currentWatermark();
}
