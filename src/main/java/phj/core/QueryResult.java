package phj.core;

import phj.join.StatsCollector;

import java.util.List;
import java.util.Map;

/** 查询输出：结果行 + 输出表头 + 执行计划 / 统计。 */
public final class QueryResult {
    public List<String> outputColumns;
    public List<Row> rows;
    public StatsCollector.Node planRoot;
    public Map<String, Object> stats;
    public String spillDir;
}
