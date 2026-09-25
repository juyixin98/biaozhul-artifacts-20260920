package dev.example.cp.core;

/**
 * 有状态算子接口（最小化的流计算库抽象）。
 *
 * <p>算子是确定性的：给定相同的事件序列和初始状态，必须产生相同的状态与输出。
 * 这使得“从检查点重放”成为可行的恢复策略。
 *
 * @param <OUT> 每条输入产生的输出类型
 */
public interface Operator<OUT> {

    /** 处理一条事件，返回它产生的输出（本参考实现每事件恰好一条输出）。 */
    OUT process(Event event);

    /** 用检查点状态覆盖当前算子状态（恢复时调用）。 */
    void restore(OperatorSnapshot snapshot);

    /** 导出当前算子状态的完整快照（检查点时调用）。 */
    OperatorSnapshot snapshot();

    /** 空状态（全新作业启动时使用）。 */
    void reset();
}
