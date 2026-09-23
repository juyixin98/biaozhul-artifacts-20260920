package com.example.quantiles.quantile;

import java.util.List;

/**
 * 精确分位数累积器（多重集合）：维护窗口内当前全部事件值的计数，
 * 支持插入、删除和按 R-7 定义查询分位数。
 *
 * <p>所有实现都必须给出与“每窗全排序”一致的<em>精确</em>结果；
 * 禁止使用任何近似草图（t-digest / KLL / HLL 等）。
 */
public interface QuantileAccumulator {

    /** 插入一个值（重复值会被重复计数）。 */
    void add(long value);

    /** 删除一个此前插入过的值；若该值计数为 0 将抛出 {@link IllegalStateException}。 */
    void remove(long value);

    /** 当前元素总数。 */
    long count();

    /**
     * 计算分位数。
     *
     * @param q 分位参数，0 &lt;= q &lt;= 1（0=最小值，1=最大值，0.5=中位数）
     * @throws IllegalArgumentException q 越界
     * @throws IllegalStateException    集合为空
     */
    Fraction quantile(double q);

    /** 批量计算多个分位数（一次排序/遍历得到全部结果）。 */
    List<Fraction> quantiles(List<Double> qs);

    /** 合并另一个累积器的全部元素到本累积器。 */
    void addAll(QuantileAccumulator other);
}
