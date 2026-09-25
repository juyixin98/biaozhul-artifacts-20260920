package com.example.cptx.core;

import java.util.EnumMap;
import java.util.Map;

/**
 * 检查点/提交流程中可注入故障的位置。协议顺序：
 *
 * <pre>
 *   1. STATE_WRITE    —— 算子状态+偏移快照原子写入（tmp → rename）的“写临时文件后、rename 前”
 *   2. OUTPUT_STAGE   —— 输出事务 staged 文件写完后、commit 前
 *   3. OUTPUT_COMMIT  —— 输出事务 rename 提交后、应用到汇总表前
 *   4. TABLE_APPLY    —— 汇总表原子重写（tmp → rename）后
 * </pre>
 *
 * 第 4 点之后故障是无害的：汇总表可由已提交输出日志在启动时完整重建。
 */
public enum FaultPhase {
    STATE_WRITE,
    OUTPUT_STAGE,
    OUTPUT_COMMIT,
    TABLE_APPLY;

    public static FaultPhase parse(String name) {
        for (FaultPhase f : values()) {
            if (f.name().equalsIgnoreCase(name)) return f;
        }
        throw new IllegalArgumentException(
                "未知故障阶段 '" + name + "'，可选: STATE_WRITE / OUTPUT_STAGE / OUTPUT_COMMIT / TABLE_APPLY");
    }
}
