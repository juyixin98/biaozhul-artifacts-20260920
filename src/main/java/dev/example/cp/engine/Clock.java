package dev.example.cp.engine;

/**
 * 可注入的时钟（默认实现为系统墙钟；测试可注入虚拟时钟做确定性推进）。
 */
@FunctionalInterface
public interface Clock {

    long nowMillis();

    Clock SYSTEM = System::currentTimeMillis;
}
