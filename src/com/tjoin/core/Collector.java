package com.tjoin.core;

/**
 * 输出收集器。库本身不依赖任何消息系统/队列，由调用方决定输出去向。
 */
@FunctionalInterface
public interface Collector {
    void collect(JoinPair pair);
}
