package com.tjoin.time;

import com.tjoin.core.Event;

/**
 * 水位线生成策略：观察事件流，产出当前水位线。
 */
public interface WatermarkGenerator {

    /** 观察一个事件（由对应输入流逐个调用）。 */
    void onEvent(Event event);

    /** 当前水位线；无观测时返回 {@link Long#MIN_VALUE}。 */
    long currentWatermark();
}
