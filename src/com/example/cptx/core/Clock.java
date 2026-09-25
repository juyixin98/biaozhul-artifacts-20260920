package com.example.cptx.core;

/**
 * 可注入的时钟。流计算库内不直接调用 System.currentTimeMillis()，
 * 而是经由本接口读取时间，使按时间触发的检查点策略可在测试中确定性推进。
 */
public interface Clock {
    long nowMillis();
}
