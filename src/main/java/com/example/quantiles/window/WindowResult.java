package com.example.quantiles.window;

import com.example.quantiles.quantile.Fraction;

import java.util.List;

/**
 * 一个滑动窗口触发时的结果。
 *
 * @param windowStartMillis 窗口起点（含），对齐到 slide 网格
 * @param windowEndMillis   窗口终点（不含）= start + size
 * @param count             窗口内事件数；为 0 时 {@link #values} 为空（但列表长度仍与请求的 q 个数一致，元素为 null）
 * @param values            各分位数值，与请求的 q 顺序一致；空窗口对应位置为 null
 */
public record WindowResult(long windowStartMillis,
                           long windowEndMillis,
                           long count,
                           List<Fraction> values) {

    public boolean isEmpty() {
        return count == 0;
    }
}
