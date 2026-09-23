package vecq;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 已校验的逻辑查询计划（不可变）。
 *
 * @param table     源表
 * @param filter    过滤计划，null 表示不过滤（全选）
 * @param projection 投影列名（保持请求顺序），空表示不输出投影结果
 * @param aggregates 聚合列表，空表示不做聚合
 * @param groupBy   分组列名（保持请求顺序），空表示无分组的全局聚合
 * @param batchSize 向量化批大小
 * @param selection 显式输入选择向量，null 表示从 [0,rowCount) 开始
 * @param exportDir 非 null 时把表/计划/结果导出到该目录
 */
public record QueryPlan(
        Table table,
        FilterExpr filter,
        List<String> projection,
        List<AggSpec> aggregates,
        List<String> groupBy,
        int batchSize,
        SelectionVector selection,
        String exportDir) {

    public static final int DEFAULT_BATCH_SIZE = 4;

    public boolean hasAggregation() {
        return !aggregates.isEmpty();
    }

    public Map<String, Object> planToJson() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("table", table.name());
        m.put("batchSize", batchSize);
        if (filter != null) m.put("filter", filter.toJson());
        m.put("projection", projection);
        List<Object> aggs = new ArrayList<>();
        for (AggSpec a : aggregates) aggs.add(a.toJson());
        m.put("aggregates", aggs);
        m.put("groupBy", groupBy);
        if (selection != null) {
            m.put("inputSelection", selection.toArray());
            m.put("inputSelectionSize", selection.size());
        }
        if (exportDir != null) m.put("exportDir", exportDir);
        return m;
    }
}
