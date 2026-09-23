package test;

import engine.executor.RequestExecutor;
import testutil.TestHarness;

/** JSON 入口端到端测试：请求解析、计划/数据导出、错误响应与退出语义。 */
public final class ExecutorTest {

    public static boolean main(String[] args) {
        TestHarness t = new TestHarness("JSON 请求入口");

        // 完整请求：分区、排序、三个函数、计划导出
        String request = """
                {
                  "includePlan": true,
                  "input": {
                    "columns": [
                      {"name": "dept", "type": "STRING"},
                      {"name": "score", "type": "LONG"}
                    ],
                    "rows": [
                      ["eng", 80],
                      ["eng", 90],
                      ["sales", 70],
                      ["eng", 80],
                      ["sales", null]
                    ]
                  },
                  "window": {
                    "partitionBy": ["dept"],
                    "orderBy": [{"column": "score", "ascending": false, "nullOrder": "NULLS_FIRST"}],
                    "functions": [
                      {"type": "ROW_NUMBER", "outputColumn": "rn"},
                      {"type": "RANK", "outputColumn": "rk"},
                      {"type": "SUM", "outputColumn": "moving_sum", "argument": "score",
                       "frame": {"mode": "ROWS",
                                 "start": {"kind": "UNBOUNDED_PRECEDING"},
                                 "end":   {"kind": "CURRENT_ROW"}}}
                    ]
                  }
                }
                """;
        RequestExecutor ex = new RequestExecutor();
        String resp = ex.execute(request);
        t.check(!ex.lastRequestFailed, "完整请求成功");
        t.check(resp.contains("\"ok\": true"), "响应 ok=true");
        t.check(resp.contains("\"plan\""), "响应包含执行计划");
        t.check(resp.contains("\"partitionBy\""), "计划含分区信息");
        t.check(resp.contains("ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"),
                "计划含可阅读的帧定义");
        t.check(resp.contains("\"moving_sum\""), "响应含结果列");
        t.check(resp.contains("\"rowCount\": 5"), "响应含行数 5");

        // 直接核对部分输出：eng 分区 DESC 排序为 90,80,80
        // RANK: 1,2,2；累计和: 90,170,250
        t.check(resp.contains("\"rk\""), "RANK 列存在");
        t.check(resp.contains("250"), "eng 组 90+80+80 累计和 250 出现");
        t.check(resp.contains("170"), "eng 组 90+80=170 出现");

        // 仅导出计划（includeData=false）
        String planOnly = new RequestExecutor().execute(
                request.replace("\"includePlan\": true",
                        "\"includePlan\": true, \"includeData\": false"));
        t.check(planOnly.contains("\"plan\"") && !planOnly.contains("\"output\""),
                "includeData=false 时只导出计划");

        // 默认 NULL 顺序：ASC 缺省 nullOrder
        String defaults = """
                {"input": {"columns": [{"name":"v","type":"LONG"}], "rows": [[1],[null]]},
                 "window": {"orderBy": [{"column": "v"}],
                            "functions": [{"type": "ROW_NUMBER", "outputColumn": "rn"}]}}
                """;
        String dResp = new RequestExecutor().execute(defaults);
        engine.json.Json.JObj dRoot = (engine.json.Json.JObj) engine.json.JsonParser.parse(dResp);
        engine.json.Json.JArr dRows = (engine.json.Json.JArr)
                ((engine.json.Json.JObj) dRoot.get("output")).get("rows");
        engine.json.Json firstCell = ((engine.json.Json.JArr) dRows.items.get(0)).items.get(0);
        engine.json.Json secondCell = ((engine.json.Json.JArr) dRows.items.get(1)).items.get(0);
        t.check(firstCell instanceof engine.json.Json.JLong j0 && j0.value() == 1L,
                "ASC 缺省 NULLS LAST：首行 = [1]");
        t.check(secondCell instanceof engine.json.Json.JNull,
                "ASC 缺省 NULLS LAST：次行 = [null]");

        // 省略 frame：SUM 默认累计帧
        String noFrame = """
                {"input": {"columns": [{"name":"v","type":"LONG"}], "rows": [[1],[2],[3]]},
                 "window": {"orderBy": [{"column": "v"}],
                            "functions": [{"type": "SUM", "outputColumn": "s", "argument": "v"}]}}
                """;
        String nf = new RequestExecutor().execute(noFrame);
        t.check(nf.contains("6"), "省略 frame 时默认累计和出现 6");

        // ---- 错误请求 ----
        expectFail(t, "{", "JSON_PARSE_ERROR", "残缺 JSON");
        expectFail(t, "[]", "INVALID_REQUEST", "顶层不是对象");
        expectFail(t, "{}", "INVALID_REQUEST", "缺少 input");
        expectFail(t, """
                {"input": {"columns": [{"name":"v","type":"FLOAT"}], "rows": []},
                 "window": {"functions": [{"type":"ROW_NUMBER","outputColumn":"rn"}]}}
                """, "INVALID_REQUEST", "不支持的列类型");
        expectFail(t, """
                {"input": {"columns": [{"name":"v","type":"LONG"}], "rows": [["x"]]},
                 "window": {"functions": [{"type":"ROW_NUMBER","outputColumn":"rn"}]}}
                """, "INVALID_REQUEST", "LONG 列收到字符串");
        expectFail(t, """
                {"input": {"columns": [{"name":"v","type":"LONG"}], "rows": [[1.5]]},
                 "window": {"functions": [{"type":"ROW_NUMBER","outputColumn":"rn"}]}}
                """, "INVALID_REQUEST", "LONG 列收到小数");
        expectFail(t, """
                {"input": {"columns": [{"name":"v","type":"LONG"}], "rows": [[1],[2]]},
                 "window": {"functions": [{"type":"SUM","outputColumn":"s","argument":"v",
                          "frame": {"start":{"kind":"FOLLOWING","offset":2},
                                    "end":{"kind":"PRECEDING","offset":1}}}]}}
                """, "INVALID_REQUEST", "非法帧（起点在终点后）");
        expectFail(t, """
                {"input": {"columns": [{"name":"v","type":"LONG"}], "rows": [[1],[2]]},
                 "window": {"functions": [{"type":"SUM","outputColumn":"v","argument":"v"}]}}
                """, "INVALID_REQUEST", "输出列重名");

        // 窗口计算溢出：错误类型为 WINDOW_ERROR
        String overflow = """
                {"input": {"columns": [{"name":"v","type":"LONG"}], "rows": [[9223372036854775807],[1]]},
                 "window": {"orderBy": [{"column": "v"}],
                            "functions": [{"type": "SUM", "outputColumn": "s", "argument": "v"}]}}
                """;
        RequestExecutor oex = new RequestExecutor();
        String or = oex.execute(overflow);
        t.check(oex.lastRequestFailed && oex.lastFailureWasWindowError,
                "溢出归类为 WINDOW_ERROR");
        t.check(or.contains("\"type\": \"WINDOW_ERROR\""), "错误响应含 WINDOW_ERROR");
        t.check(or.contains("溢出"), "错误信息说明溢出");

        // 非溢出失败不应标记为窗口错误
        RequestExecutor bex = new RequestExecutor();
        bex.execute("{");
        t.check(bex.lastRequestFailed && !bex.lastFailureWasWindowError,
                "JSON 错误不是窗口错误");

        return t.report();
    }

    private static void expectFail(TestHarness t, String request, String type, String label) {
        RequestExecutor ex = new RequestExecutor();
        String resp = ex.execute(request);
        t.check(ex.lastRequestFailed, label + "：请求应失败");
        t.check(resp.contains("\"type\": \"" + type + "\""),
                label + "：错误类型 " + type + "，响应=" + resp.replaceAll("\\s+", " "));
    }
}
