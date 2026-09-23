package engine.window;

import java.util.List;

/**
 * 一个窗口的完整规格：分区键、排序键、函数列表。
 *
 * <p>排序键为空时：RANK 恒为 1；ROW_NUMBER 按输入原始顺序编号；
 * SUM 的帧按输入顺序展开。
 */
public record WindowSpec(
        List<String> partitionBy,
        List<OrderKey> orderBy,
        List<FunctionSpec> functions) {

    public WindowSpec {
        partitionBy = List.copyOf(partitionBy);
        orderBy = List.copyOf(orderBy);
        functions = List.copyOf(functions);
    }
}
