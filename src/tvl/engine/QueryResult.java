package tvl.engine;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 查询执行结果：输出列名、数据行（值可为 null）、执行计划统计。 */
public final class QueryResult {
    private final List<String> columns;
    private final List<Row> rows;
    private final PlanNode plan;
    private final long scannedRows;
    private final long matchedRows;

    public QueryResult(List<String> columns, List<Row> rows, PlanNode plan,
                       long scannedRows, long matchedRows) {
        this.columns = columns;
        this.rows = rows;
        this.plan = plan;
        this.scannedRows = scannedRows;
        this.matchedRows = matchedRows;
    }

    public List<String> columns() {
        return columns;
    }

    public List<Row> rows() {
        return rows;
    }

    public PlanNode plan() {
        return plan;
    }

    public long scannedRows() {
        return scannedRows;
    }

    public long matchedRows() {
        return matchedRows;
    }

    public int returnedRows() {
        return rows.size();
    }

    /** 转为可 JSON 序列化的结构。 */
    public Map<String, Object> toJson() {
        Map<String, Object> json = new LinkedHashMap<>();
        json.put("ok", true);
        json.put("columns", new ArrayList<>(columns));

        List<Object> rowsJson = new ArrayList<>();
        for (Row row : rows) {
            Map<String, Object> r = new LinkedHashMap<>();
            for (String col : columns) {
                r.put(col, row.get(col));
            }
            rowsJson.add(r);
        }
        json.put("rows", rowsJson);
        json.put("rowCount", (long) rows.size());

        Map<String, Object> stats = new LinkedHashMap<>();
        stats.put("scanned", scannedRows);
        stats.put("matched", matchedRows);
        stats.put("returned", (long) rows.size());
        json.put("stats", stats);
        json.put("plan", plan.toJson());
        return json;
    }
}
