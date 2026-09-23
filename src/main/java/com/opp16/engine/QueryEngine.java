package com.opp16.engine;

import com.opp16.engine.json.Json;

import java.util.ArrayList;
import java.util.List;

/**
 * Request orchestration: parse table + plan, run the vectorised engine,
 * optionally cross-check against the row interpreter, and build the JSON
 * response (plan, data, selection vector, batches and result are all
 * exportable through the response and/or the CLI --export-* flags).
 */
public final class QueryEngine {

    public static final class Response {
        public final Json.Obj json;
        public final int status; // 0 ok, 2 request error

        Response(Json.Obj json, int status) { this.json = json; this.status = status; }
        String render() { return json.render(true); }
    }

    public Response handle(String requestText) {
        try {
            Json.Value parsed = Json.parse(requestText);
            if (!parsed.isObject()) throw new EngineException("request root must be an object");
            Json.Obj req = parsed.asObject();

            if (!req.has("table")) throw new EngineException("request missing 'table'");
            Table table = Table.fromJson(req.get("table").asObject());
            QueryPlan plan = QueryPlan.fromRequest(req);
            return run(table, plan);
        } catch (EngineException e) {
            return error(e.getMessage());
        } catch (Json.JsonException e) {
            return error(e.getMessage());
        } catch (Exception e) {
            return error("internal error: " + e);
        }
    }

    public Response run(Table table, QueryPlan plan) {
        ColumnarExecutor exec = new ColumnarExecutor(table, plan);
        ColumnarExecutor.SelectionWithStats ss = exec.buildSelection();
        ColumnarExecutor.Result result = exec.executeWith(ss.selection);
        Json.Obj root = new Json.Obj();
        if (plan.requestId != null) root.put("requestId", plan.requestId);
        root.put("ok", true);
        root.put("plan", Json.of(plan.describe().trim()));

        Json.Obj stats = new Json.Obj();
        stats.put("mode", plan.explicitSelection != null ? "explicit_selection" : "filter");
        stats.put("tableRows", table.rowCount());
        stats.put("selectedRows", ss.selection.size());
        stats.put("outputRows", result.rows.size());
        stats.put("batches", ss.stats.toJson());
        root.put("stats", stats);

        root.put("selection", ss.selection.toJson());
        root.put("result", resultToJson(result));

        // The raw inputs are exportable too; the CLI writes these to files.
        root.put("data", table.toJson());

        // Cross-check against the row-at-a-time reference interpreter: both
        // selection vectors and materialised results must agree.
        RowInterpreter ref = new RowInterpreter(table, plan);
        ColumnarExecutor.Result refResult = ref.execute();
        SelectionVector refSel = ref.buildSelection();
        root.put("crossCheck", crossCheckJson(result, refResult, ss.selection, refSel));
        return new Response(root, 0);
    }

    private Json.Obj crossCheckJson(ColumnarExecutor.Result got, ColumnarExecutor.Result ref,
                                    SelectionVector gotSel, SelectionVector refSel) {
        Json.Obj cc = new Json.Obj();
        boolean selEqual = java.util.Arrays.equals(gotSel.toArray(), refSel.toArray());
        boolean rowsEqual = resultsEqual(got, ref);
        cc.put("selectionMatch", selEqual);
        cc.put("resultMatch", rowsEqual);
        cc.put("rowInterpreterSelection", refSel.toJson());
        cc.put("rowInterpreterResult", resultToJson(ref));
        cc.put("passed", selEqual && rowsEqual);
        return cc;
    }

    /** Structural comparison of two results incl. column names, types and values. */
    public static boolean resultsEqual(ColumnarExecutor.Result a, ColumnarExecutor.Result b) {
        if (!a.columns.equals(b.columns)) return false;
        if (!a.types.equals(b.types)) return false;
        if (a.rows.size() != b.rows.size()) return false;
        for (int i = 0; i < a.rows.size(); i++) {
            Object[] x = a.rows.get(i), y = b.rows.get(i);
            if (x.length != y.length) return false;
            for (int k = 0; k < x.length; k++) {
                if (x[k] == null || y[k] == null) {
                    if (x[k] != y[k]) return false;
                } else if (x[k] instanceof Double || y[k] instanceof Double) {
                    double dx = ((Number) x[k]).doubleValue();
                    double dy = ((Number) y[k]).doubleValue();
                    if (Double.compare(dx, dy) != 0) return false;
                } else if (!x[k].equals(y[k])) {
                    return false;
                }
            }
        }
        return true;
    }

    public static Json.Obj resultToJson(ColumnarExecutor.Result r) {
        Json.Obj out = new Json.Obj();
        Json.Arr schema = new Json.Arr();
        for (int i = 0; i < r.columns.size(); i++) {
            Json.Obj c = new Json.Obj();
            c.put("name", r.columns.get(i));
            c.put("type", r.types.get(i).name());
            schema.add(c);
        }
        out.put("columns", schema);
        Json.Arr rows = new Json.Arr();
        for (Object[] row : r.rows) {
            Json.Arr arr = new Json.Arr();
            for (Object v : row) arr.add(boxToJson(v));
            rows.add(arr);
        }
        out.put("rows", rows);
        out.put("rowCount", r.rows.size());
        return out;
    }

    private static Json.Value boxToJson(Object v) {
        if (v == null) return Json.NULL;
        if (v instanceof Long x) return new Json.Num(x);
        if (v instanceof Integer x) return new Json.Num(x.longValue());
        if (v instanceof Double x) return new Json.Num(x);
        if (v instanceof String x) return new Json.Str(x);
        throw new EngineException("internal: cannot serialise " + v.getClass());
    }

    public Response error(String message) {
        Json.Obj root = new Json.Obj();
        root.put("ok", false);
        root.put("error", Json.of(message));
        return new Response(root, 2);
    }
}
