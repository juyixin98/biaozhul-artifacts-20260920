package sessions.agg;

/**
 * 可注入的聚合函数。聚合值用 {@code long} 表示，
 * 既能表达 COUNT 也能表达 SUM（样本数据范围内）。
 *
 * @param <A> 累加器类型（离线参考实现做全量分组时使用）
 */
public interface AggregateFunction<A> {

    /** 空窗口/空分组的初值。 */
    long emptyResult();

    /** 单个事件并入 long 结果（COUNT 忽略 value 直接 +1；SUM 加上 value）。 */
    long add(long result, long value);

    /** 把一个事件值并入累加器（参考实现使用）。 */
    A add(A accumulator, long value);

    /** 从累加器输出最终 long 结果。 */
    long result(A accumulator);

    /** 合并两个 long 结果（流式算子合并窗口时使用）。 */
    long merge(long a, long b);
}
