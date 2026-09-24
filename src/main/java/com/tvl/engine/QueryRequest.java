package com.tvl.engine;

import com.tvl.columnar.Batch;

import java.util.List;

/**
 * 一次查询的输入：SQL、参数（与 SQL 中 ? 按位置对应）、列批次序列。
 * 参数类型由 paramTypes 显式给出 —— 参数是强类型绑定，不靠 JSON 值猜测。
 */
public record QueryRequest(
        String sql,
        List<com.tvl.types.DataType> paramTypes,
        List<Object> params,
        List<Batch> batches,
        boolean crossCheck
) {
    public QueryRequest {
        paramTypes = paramTypes == null ? List.of() : List.copyOf(paramTypes);
        // 参数值允许为 null（SQL NULL 参数），不能用 List.copyOf（它拒绝 null）
        params = params == null ? List.of() : java.util.Collections.unmodifiableList(
                new java.util.ArrayList<>(params));
        batches = batches == null ? List.of() : List.copyOf(batches);
    }
}
