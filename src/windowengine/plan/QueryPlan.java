package windowengine.plan;

import java.util.List;

/**
 * 查询执行计划（逻辑即物理：单机、内存、一次扫描分区）。
 *
 * @param input     输入关系名（仅用于导出/展示，执行时直接带数据）
 * @param window    窗口规格（分区 / 排序 / 帧）
 * @param functions 需要计算的窗口函数列表，输出列按列表顺序追加
 */
public record QueryPlan(String input,
                        WindowSpec window,
                        List<FunctionCall> functions) {
}
