package windowengine;

import windowengine.plan.QueryPlan;

/**
 * 解析后的完整请求：输入关系 + 执行计划 + 可选导出目录。
 */
public record QueryRequest(Relation data, QueryPlan plan, String exportDir) {
}
