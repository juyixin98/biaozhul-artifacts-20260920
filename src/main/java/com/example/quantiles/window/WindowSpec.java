package com.example.quantiles.window;

import java.util.List;

/**
 * 滑动窗口配置。
 *
 * <p>窗口为半开区间 {@code [start, start+sizeMillis)}；窗口起点对齐到 slide 网格：
 * {@code startMillis = floorDiv(ts, slideMillis) * slideMillis}，
 * 时间戳为负时网格仍然连续正确。
 *
 * @param sizeMillis           窗口长度，必须为正
 * @param slideMillis          滑动步长，必须为正且整除 sizeMillis
 * @param quantiles            每个窗口要计算的分位参数 q（每个满足 0 &lt;= q &lt;= 1）
 * @param allowedLatenessMillis 允许迟到：窗口在 watermark 到达 {@code end + allowedLateness}
 *                             时触发；迟到（所属所有 pane 已触发）的事件被丢弃并计数
 */
public record WindowSpec(long sizeMillis,
                         long slideMillis,
                         List<Double> quantiles,
                         long allowedLatenessMillis) {

    public WindowSpec {
        if (sizeMillis <= 0) {
            throw new IllegalArgumentException("sizeMillis 必须为正: " + sizeMillis);
        }
        if (slideMillis <= 0) {
            throw new IllegalArgumentException("slideMillis 必须为正: " + slideMillis);
        }
        if (sizeMillis % slideMillis != 0) {
            throw new IllegalArgumentException(
                    "sizeMillis 必须是 slideMillis 的整数倍: size=" + sizeMillis
                            + " slide=" + slideMillis);
        }
        if (quantiles == null || quantiles.isEmpty()) {
            throw new IllegalArgumentException("quantiles 不能为空");
        }
        quantiles = List.copyOf(quantiles);
        for (double q : quantiles) {
            if (Double.isNaN(q) || q < 0.0 || q > 1.0) {
                throw new IllegalArgumentException("q 必须在 [0,1] 内: " + q);
            }
        }
        if (allowedLatenessMillis < 0) {
            throw new IllegalArgumentException(
                    "allowedLatenessMillis 不能为负: " + allowedLatenessMillis);
        }
    }

    /** 每个窗口包含的 pane 数。 */
    public long panesPerWindow() {
        return sizeMillis / slideMillis;
    }

    /** 给定事件时间戳，返回它落入的 pane 的起点（网格对齐，负时间戳安全）。 */
    public long paneStartForTimestamp(long timestampMillis) {
        return Math.floorDiv(timestampMillis, slideMillis) * slideMillis;
    }
}
