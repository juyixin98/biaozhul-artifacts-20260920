package com.example.bitemporal.model;

/**
 * 写入模式。
 * <ul>
 *   <li>{@link #INSERT}：仅在业务时间轴的空白处登记新事实；与当前版本重叠则拒绝。</li>
 *   <li>{@link #CORRECTION}：追溯修订，重述历史——重叠的旧版本被关闭保留，
 *       被覆盖区间以外的部分拆分续存。</li>
 * </ul>
 */
public enum WriteMode {
    INSERT,
    CORRECTION
}
