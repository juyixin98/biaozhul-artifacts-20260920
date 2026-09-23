package tvl.engine;

import java.util.List;

import tvl.expr.Expr;

/**
 * 一次查询的已解析规格（由 API 层从 JSON 构造并完成类型检查）。
 *
 * @param table      要扫描的表
 * @param filter     WHERE 表达式，null 表示无条件
 * @param filterText WHERE 表达式原文（用于执行计划展示）
 * @param select     输出列；null 或包含 "*" 表示全部列
 * @param limit      行数上限，null 表示不限制
 */
public record QuerySpec(String table, Expr filter, String filterText,
                        List<String> select, Long limit) {
}
