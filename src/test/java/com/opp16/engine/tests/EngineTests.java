package com.opp16.engine.tests;

import com.opp16.engine.EngineException;
import com.opp16.engine.QueryEngine;
import com.opp16.engine.json.Json;

/** End-to-end tests through the JSON request entry point. */
public final class EngineTests {

    private EngineTests() {}

    private static final String TABLE_JSON = """
            {
              "columns": [
                {"name": "id",    "type": "INT",    "values": [1, 2, 3, 4, 5, 6, 7, 8]},
                {"name": "dept",  "type": "STRING", "values": ["eng", "sales", "eng", null, "sales", "eng", null, "eng"]},
                {"name": "score", "type": "INT",    "values": [10, 5, null, 20, 5, 10, 7, null]}
              ]
            }
            """;

    /** Build a request from a body fragment, injecting the shared table definition. */
    private static String req(String body) {
        Json.Obj o = new Json.Obj();
        o.put("table", Json.parse(TABLE_JSON));
        if (body != null && !body.isBlank()) {
            Json.Obj extra = Json.parse("{" + body + "}").asObject();
            for (var e : extra.map.entrySet()) o.put(e.getKey(), e.getValue());
        }
        return o.render();
    }

    private static Json.Obj run(String request) {
        QueryEngine.Response r = new QueryEngine().handle(request);
        if (r.status != 0) {
            throw new EngineException("request failed: " + r.json.get("error").asString());
        }
        return r.json;
    }

    /** Returns "OK" or the error message (for failure-path tests). */
    private static String status(String request) {
        QueryEngine.Response r = new QueryEngine().handle(request);
        return r.status == 0 ? "OK" : r.json.get("error").asString();
    }

    private static Json.Arr rows(Json.Obj response) {
        return response.get("result").asObject().get("rows").asArray();
    }

    private static boolean passed(Json.Obj r) {
        return r.get("crossCheck").asObject().get("passed").asBoolean();
    }

    private static long batchCount(Json.Obj r) {
        return r.get("stats").asObject().get("batches").asObject().get("batchCount").asLong();
    }

    private static String selectedPerBatch(Json.Obj r) {
        return r.get("stats").asObject().get("batches").asObject().get("batches").asArray()
                .list.stream().map(v -> String.valueOf(v.asObject().get("selected").asLong())).toList()
                .toString();
    }

    public static void run() {
        Assert.suite("Engine end-to-end");

        // ---- basic filter + projection, cross-checked against interpreter ----
        Json.Obj r1 = run(req("""
                "batchSize": 3,
                "filter": {"op": ">=", "column": "score", "value": 10},
                "project": ["id", "score"]
                """));
        Assert.eq("filter score>=10 selects ids 1,4,6",
                rows(r1).render(), "[[1,10],[4,20],[6,10]]");
        Assert.check("crossCheck passed (selection+result)", passed(r1));
        Assert.eq("3 batches reported for 8 rows with batchSize 3", batchCount(r1), 3L);
        // batch0 rows 0..2: id=1 score=10 -> 1 survivor;
        // batch1 rows 3..5: id=4 score=20, id=6 score=10 -> 2; batch2: 0
        Assert.eq("batch survivors 1,2,0", selectedPerBatch(r1), "[1, 2, 0]");

        // ---- NULL handling: WHERE score = 5 must exclude NULL scores ----
        Json.Obj r2 = run(req("""
                "filter": {"op": "=", "column": "score", "value": 5}
                """));
        Assert.eq("score=5 rows are 2 and 5", rows(r2).render(),
                "[[2,\"sales\",5],[5,\"sales\",5]]");
        Assert.check("crossCheck score=5", passed(r2));

        // ---- IS NULL keeps all-null rows ----
        Json.Obj r3 = run(req("""
                "filter": {"op": "is_null", "column": "score"}, "project": ["id"]
                """));
        Assert.eq("score IS NULL -> ids 3,8", rows(r3).render(), "[[3],[8]]");

        // ---- three-valued logic: NULL dept fails '=' but IS NULL succeeds ----
        Json.Obj r4 = run(req("""
                "filter": {"op": "!=", "column": "dept", "value": "eng"}
                """));
        Assert.eq("dept!=eng gives sales only (nulls excluded)",
                rows(r4).render(), "[[2,\"sales\",5],[5,\"sales\",5]]");

        Json.Obj r5 = run(req("""
                "filter": {"op":"or","args":[
                    {"op":"!=","column":"dept","value":"eng"},
                    {"op":"is_null","column":"dept"}]}
                """));
        Assert.eq("dept!=eng OR dept IS NULL -> 2,4,5,7",
                rows(r5).render(),
                "[[2,\"sales\",5],[4,null,20],[5,\"sales\",5],[7,null,7]]");

        // ---- explicit sparse selection with duplicates, then project ----
        Json.Obj sparse = run(req("""
                "selection": [1, 1, 4, 4, 4, 2], "project": ["id", "dept"]
                """));
        Assert.eq("duplicated sparse selection materialised repeatedly",
                rows(sparse).render(),
                "[[2,\"sales\"],[2,\"sales\"],[5,\"sales\"],[5,\"sales\"],[5,\"sales\"],[3,\"eng\"]]");
        Assert.check("explicit selection mode flagged",
                "explicit_selection".equals(r1.get("stats").asObject().get("mode").asString()) == false);
        Assert.check("sparse mode flagged",
                "explicit_selection".equals(sparse.get("stats").asObject().get("mode").asString()));

        // ---- invalid explicit subscripts: request error ----
        String err = status(req("\"selection\": [0, 8, 1]"));
        Assert.check("subscript 8 on 8-row table rejected: " + err,
                err.contains("invalid selection subscript 8"));
        String errNeg = status(req("\"selection\": [-1]"));
        Assert.check("negative subscript rejected: " + errNeg,
                errNeg.contains("invalid selection subscript -1"));

        // ---- aggregation: count(*) vs count(col) differ under NULLs ----
        Json.Obj agg = run(req("""
                "aggregate": [
                    {"fn":"count"},
                    {"fn":"count", "column":"score", "alias":"n_score"},
                    {"fn":"sum", "column":"score", "alias":"sum_score"},
                    {"fn":"avg", "column":"score", "alias":"avg_score"},
                    {"fn":"min", "column":"score", "alias":"min_score"},
                    {"fn":"max", "column":"score", "alias":"max_score"}
                  ]
                """));
        Assert.eq("global aggregates over 8 rows", rows(agg).render(),
                "[[8,6,57,9.5,5,20]]");
        Assert.check("aggregates cross-checked", passed(agg));

        // ---- duplicates feed aggregates: selection [5,5,5] -> index 5 is id=6 score=10 ----
        Json.Obj dupAgg = run(req("""
                "selection": [5, 5, 5],
                "aggregate": [{"fn":"count"}, {"fn":"sum","column":"score","alias":"s"},
                              {"fn":"avg","column":"score","alias":"a"}]
                """));
        // AVG renders as JSON number 10 (10.0 is the same JSON numeric value)
        Assert.eq("duplicate indices count three times", rows(dupAgg).render(), "[[3,30,10]]");
        Json.Value avgRendered = rows(dupAgg).get(0).asArray().get(2);
        Assert.check("avg value is numeric and decimal-typed",
                avgRendered.isNumber() && ((Json.Num) avgRendered).decimal
                        && avgRendered.asDouble() == 10.0);

        // ---- GROUP BY incl. NULL group; projection reorders/drops columns ----
        Json.Obj grp = run(req("""
                "groupBy": ["dept"],
                "aggregate": [{"fn":"count"}, {"fn":"sum","column":"id","alias":"id_sum"}],
                "project": ["dept", "id_sum"]
                """));
        Assert.eq("group by dept with null group, projected/reordered",
                rows(grp).render(), "[[\"eng\",18],[\"sales\",7],[null,11]]");
        Assert.check("group-by cross-check", passed(grp));

        Json.Obj emptyAgg = run(req("""
                "filter": {"op":">","column":"id","value":1000},
                "aggregate": [{"fn":"count"}, {"fn":"sum","column":"score","alias":"s"},
                              {"fn":"avg","column":"score","alias":"a"}]
                """));
        Assert.eq("empty filter aggregate: one row, count 0, others null",
                rows(emptyAgg).render(), "[[0,null,null]]");

        // ---- all-NULL column table ----
        String allNullReq = """
                {
                  "table": {"columns": [
                    {"name":"k","type":"INT","values":[1,2,3,4,5]},
                    {"name":"v","type":"INT","values":[null,null,null,null,null]}
                  ]},
                  "filter": {"op":">","column":"v","value":0},
                  "aggregate": [{"fn":"count"}, {"fn":"sum","column":"v","alias":"sv"},
                                {"fn":"avg","column":"v","alias":"av"}]
                }
                """;
        Json.Obj an = run(allNullReq);
        Assert.eq("all-null column filter+aggregate", rows(an).render(), "[[0,null,null]]");
        Assert.check("all-null cross-check", passed(an));

        String allNullProject = """
                {
                  "table": {"columns": [
                    {"name":"k","type":"INT","values":[1,2,3,4,5]},
                    {"name":"v","type":"INT","values":[null,null,null,null,null]}
                  ]},
                  "filter": {"op":"is_null","column":"v"},
                  "project": ["k", "v"]
                }
                """;
        Json.Obj anp = run(allNullProject);
        Assert.eq("is_null on all-null column returns all rows",
                rows(anp).render(), "[[1,null],[2,null],[3,null],[4,null],[5,null]]");

        // ---- empty table ----
        String emptyReq = """
                {
                  "table": {"columns": [
                    {"name":"k","type":"INT","values":[]},
                    {"name":"s","type":"STRING","values":[]}
                  ]},
                  "batchSize": 4,
                  "filter": {"op":">","column":"k","value":0},
                  "aggregate": [{"fn":"count"}]
                }
                """;
        Json.Obj et = run(emptyReq);
        Assert.eq("empty table count is 0", rows(et).render(), "[[0]]");
        Assert.eq("empty table still reports one batch", batchCount(et), 1L);

        // ---- malformed / semantically invalid requests ----
        Assert.check("malformed JSON rejected", status("{not json").contains("JSON error"));
        Assert.check("missing table rejected",
                new QueryEngine().handle("{\"batchSize\":1}").json.get("error").asString()
                        .contains("missing 'table'"));
        Assert.check("bad batchSize rejected",
                status(req("\"batchSize\": 0")).contains("batchSize"));
        Assert.check("unknown column rejected",
                status(req("\"filter\": {\"op\":\">\",\"column\":\"nope\",\"value\":1}"))
                        .contains("unknown column"));
        Assert.check("type mismatch rejected (STRING col, INT literal)",
                status(req("\"filter\": {\"op\":\">\",\"column\":\"dept\",\"value\":1}"))
                        .contains("not INT"));
        Assert.check("null literal compare rejected with guidance",
                status(req("\"filter\": {\"op\":\"=\",\"column\":\"dept\",\"value\":null}"))
                        .contains("is_null"));
        Assert.check("filter + explicit selection together rejected",
                status(req("\"selection\":[0], \"filter\": {\"op\":\">\",\"column\":\"id\",\"value\":0}"))
                        .contains("cannot both be set"));
        Assert.check("unknown projection column rejected",
                status(req("\"project\": [\"nope\"]")).contains("unknown column"));
        Assert.check("unknown aggregate function rejected",
                status(req("\"aggregate\": [{\"fn\":\"median\",\"column\":\"score\"}]"))
                        .contains("unknown aggregate"));

        // ---- export payloads present in response ----
        Json.Obj exp = run(req("""
                "explain": true, "filter": {"op":">","column":"id","value":6}
                """));
        String plan = exp.get("plan").asString();
        Assert.check("plan export contains Project/Filter/TableScan",
                plan.contains("Project") && plan.contains("Filter") && plan.contains("TableScan"));
        Assert.check("data export round-trips rowCount",
                exp.get("data").asObject().get("rowCount").asLong() == 8L);
        // id > 6 matches ids 7,8 at zero-based subscripts 6,7
        Assert.eq("selection export", exp.get("selection").asArray().render(), "[6,7]");
    }
}
