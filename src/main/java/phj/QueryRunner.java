package phj;

import phj.core.QueryRequest;
import phj.core.QueryResult;
import phj.core.Row;
import phj.core.Value;
import phj.join.DiskQuotaException;
import phj.join.HashJoinEngine;
import phj.json.Json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 查询执行入口：吃请求 JSON（Map），吐响应 JSON（Map）。
 * CLI 与 HTTP Server 共用这一层。
 */
public final class QueryRunner {

    private QueryRunner() {}

    /** 成功响应。 */
    public static Map<String, Object> run(Map<String, Object> request) {
        QueryRequest req = QueryRequest.fromJson(request);
        QueryResult result = new HashJoinEngine(req).execute();
        return successResponse(req, result);
    }

    /** 统一异常映射（HTTP 状态码语义复用：quota -> 507，请求错误 -> 400）。 */
    public static Map<String, Object> errorResponse(Throwable t) {
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("ok", false);
        String type;
        int code;
        if (t instanceof DiskQuotaException) {
            type = "DISK_QUOTA_EXCEEDED";
            code = 507;
            DiskQuotaException q = (DiskQuotaException) t;
            resp.put("diskQuotaBytes", q.limitBytes());
            resp.put("usedBytes", q.usedBytes());
        } else if (t instanceof IllegalArgumentException || t instanceof phj.json.JsonException) {
            type = "INVALID_REQUEST";
            code = 400;
        } else {
            type = "INTERNAL_ERROR";
            code = 500;
        }
        resp.put("errorType", type);
        resp.put("errorCode", code);
        resp.put("error", t.getMessage() == null ? t.getClass().getSimpleName() : t.getMessage());
        return resp;
    }

    private static Map<String, Object> successResponse(QueryRequest req, QueryResult result) {
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("ok", true);

        List<String> cols = result.outputColumns;
        // 预先计算重名列的展示名：重名时加 #位置 后缀，保证 asObject 的键唯一
        Map<String, Integer> freq = new LinkedHashMap<>();
        for (String c : cols) freq.merge(c, 1, Integer::sum);
        String[] objKeys = new String[cols.size()];
        for (int i = 0; i < cols.size(); i++) {
            String c = cols.get(i);
            objKeys[i] = freq.get(c) == 1 ? c : c + "#" + i;
        }
        List<Object> rows = new ArrayList<>(result.rows.size());
        for (Row r : result.rows) {
            Map<String, Object> rowMap = new LinkedHashMap<>();
            List<Object> rowArr = new ArrayList<>(r.width());
            for (int i = 0; i < r.width(); i++) {
                Value v = r.get(i);
                rowMap.put(objKeys[i], v.toJson());
                rowArr.add(v.toJson());
            }
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("values", rowArr);
            row.put("asObject", rowMap);
            rows.add(row);
        }

        Map<String, Object> data = new LinkedHashMap<>();
        data.put("columns", cols);
        data.put("rowCount", result.rows.size());
        data.put("rows", rows);
        resp.put("result", data);

        if (req.exportPlan) {
            Map<String, Object> plan = new LinkedHashMap<>();
            plan.put("plan", result.planRoot.toMap());
            plan.put("stats", result.stats);
            if (result.spillDir != null) plan.put("spillDir", result.spillDir);
            resp.put("executionPlan", plan);
        }
        return resp;
    }

    public record Executed(String json, int exitCode) {}

    /** 执行原始请求 JSON，返回响应 JSON 与进程退出码。 */
    public static Executed executeJson(String requestJson) {
        Map<String, Object> resp;
        int exitCode;
        try {
            Map<String, Object> req = Json.asObj(Json.parse(requestJson), "请求");
            resp = run(req);
            exitCode = 0;
        } catch (Throwable t) {
            resp = errorResponse(t);
            Object code = resp.get("errorCode");
            exitCode = code instanceof Number n ? (n.intValue() == 507 ? 3 : n.intValue() == 400 ? 2 : 1) : 1;
        }
        return new Executed(Json.writePretty(resp), exitCode);
    }

    public static String runJson(String requestJson) {
        return executeJson(requestJson).json();
    }
}
