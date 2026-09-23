package partjoin;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Translates a JSON request into a {@link HashJoinEngine} run and builds the
 * JSON response. Tables may be inline ("rows") or reference a JSON/JSONL file
 * ("file": {"path": ..., "format": "json"|"jsonl"}).
 *
 * Request shape:
 * {
 *   "joinType": "INNER" | "LEFT",
 *   "left":  {"name":..., "columns":[...], "rows":[[...], ...] | "file":{...}},
 *   "right": {...},
 *   "leftKeys": ["col", ...],
 *   "rightKeys": ["col", ...],
 *   "options": {
 *     "inMemoryRows": 1024,
 *     "diskBudgetBytes": -1,            // -1 or omitted = unlimited
 *     "maxRepartitionDepth": 2,
 *     "spillDir": "/tmp/...",
 *     "maxInlineRows": 10000,           // rows echoed into the response
 *     "output": {"path": "...", "format": "jsonl"|"json"},
 *     "planOutput": {"path": "..."}
 *   }
 * }
 */
public final class RequestRunner {

    private RequestRunner() {
    }

    public static Map<String, Object> run(Map<String, Object> req) {
        JoinType type = JoinType.parse(Json.optString(req, "joinType", "INNER"));
        Table left = parseTable(Json.getObject(req, "left"));
        Table right = parseTable(Json.getObject(req, "right"));
        List<String> leftKeys = Json.getStringList(req, "leftKeys");
        List<String> rightKeys = Json.getStringList(req, "rightKeys");

        Map<String, Object> opts = req.get("options") instanceof Map m
                ? castMap(m) : new LinkedHashMap<>();
        HashJoinEngine.Config cfg = new HashJoinEngine.Config();
        cfg.inMemoryRows = Json.optInt(opts, "inMemoryRows", cfg.inMemoryRows);
        long budget = Json.optLong(opts, "diskBudgetBytes", Long.MAX_VALUE);
        cfg.diskBudgetBytes = budget < 0 ? Long.MAX_VALUE : budget;
        cfg.maxRepartitionDepth = Json.optInt(opts, "maxRepartitionDepth", cfg.maxRepartitionDepth);
        cfg.spillDir = Json.optString(opts, "spillDir", cfg.spillDir);
        int maxInline = Json.optInt(opts, "maxInlineRows", 10_000);

        HashJoinEngine engine = new HashJoinEngine(left, right, type, leftKeys, rightKeys, cfg);

        Map<String, Object> outputOpt = opts.get("output") instanceof Map m ? castMap(m) : null;
        RowCollector.FileCollector fileCollector = null;
        RowCollector.ListCollector listCollector = null;
        RowCollector collector;
        Path outputPath = null;
        String outputFormat = "jsonl";
        if (outputOpt != null) {
            outputPath = Path.of(Json.getString(outputOpt, "path"));
            outputFormat = Json.optString(outputOpt, "format", "jsonl");
            if (!outputFormat.equalsIgnoreCase("jsonl") && !outputFormat.equalsIgnoreCase("json")) {
                throw new JoinException(JoinException.INVALID_REQUEST,
                        "output.format must be 'jsonl' or 'json'");
            }
            fileCollector = new RowCollector.FileCollector(outputPath, outputFormat);
            collector = fileCollector;
        } else {
            listCollector = new RowCollector.ListCollector();
            collector = listCollector;
        }

        HashJoinEngine.EngineOut out;
        try {
            out = engine.execute(collector);
        } finally {
            if (fileCollector != null) fileCollector.close();
        }

        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("ok", true);
        resp.put("plan", out.plan);
        resp.put("stats", out.stats);

        if (fileCollector != null) {
            Map<String, Object> loc = new LinkedHashMap<>();
            loc.put("path", outputPath.toAbsolutePath().toString());
            loc.put("format", outputFormat.toLowerCase());
            loc.put("rows", fileCollector.count());
            resp.put("output", loc);
        } else {
            List<Row> rows = listCollector.rows();
            Map<String, Object> inline = new LinkedHashMap<>();
            List<String> outCols = new ArrayList<>();
            outCols.addAll(left.schema().columns());
            outCols.addAll(right.schema().columns());
            inline.put("columns", outCols);
            boolean truncated = rows.size() > maxInline;
            List<Object> arr = new ArrayList<>();
            int limit = Math.min(rows.size(), maxInline);
            for (int i = 0; i < limit; i++) arr.add(rows.get(i).rawList());
            inline.put("rows", arr);
            inline.put("rowCount", rows.size());
            inline.put("truncated", truncated);
            resp.put("output", inline);
        }

        if (opts.get("planOutput") instanceof Map po) {
            Path planPath = Path.of(Json.getString(castMap(po), "path"));
            writePlanFile(planPath, out.plan, out.stats);
            resp.put("planOutput", planPath.toAbsolutePath().toString());
        }
        return resp;
    }

    /** Plan-only mode: validates the request and exports the plan without running. */
    public static Map<String, Object> plan(Map<String, Object> req) {
        JoinType type = JoinType.parse(Json.optString(req, "joinType", "INNER"));
        Table left = parseTable(Json.getObject(req, "left"));
        Table right = parseTable(Json.getObject(req, "right"));
        List<String> leftKeys = Json.getStringList(req, "leftKeys");
        List<String> rightKeys = Json.getStringList(req, "rightKeys");
        Map<String, Object> opts = req.get("options") instanceof Map m
                ? castMap(m) : new LinkedHashMap<>();
        HashJoinEngine.Config cfg = new HashJoinEngine.Config();
        cfg.inMemoryRows = Json.optInt(opts, "inMemoryRows", cfg.inMemoryRows);
        long budget = Json.optLong(opts, "diskBudgetBytes", Long.MAX_VALUE);
        cfg.diskBudgetBytes = budget < 0 ? Long.MAX_VALUE : budget;
        cfg.maxRepartitionDepth = Json.optInt(opts, "maxRepartitionDepth", cfg.maxRepartitionDepth);
        cfg.spillDir = Json.optString(opts, "spillDir", cfg.spillDir);

        HashJoinEngine engine = new HashJoinEngine(left, right, type, leftKeys, rightKeys, cfg);
        Map<String, Object> plan = engine.explain();
        if (opts.get("planOutput") instanceof Map po) {
            writePlanFile(Path.of(Json.getString(castMap(po), "path")), plan, null);
        }
        return plan;
    }

    private static void writePlanFile(Path path, Map<String, Object> plan,
                                      Map<String, Object> stats) {
        Map<String, Object> doc = new LinkedHashMap<>();
        doc.put("plan", plan);
        if (stats != null) doc.put("stats", stats);
        try {
            if (path.getParent() != null) Files.createDirectories(path.getParent());
            Files.writeString(path, Json.writePretty(doc), StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new JoinException(JoinException.IO_ERROR,
                    "Cannot write plan file: " + path + " (" + e.getMessage() + ")", e);
        }
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> castMap(Map<?, ?> m) {
        return (Map<String, Object>) m;
    }

    // ------------------------------------------------------------------
    // Table loading
    // ------------------------------------------------------------------

    private static Table parseTable(Map<String, Object> t) {
        String name = Json.optString(t, "name", "");
        List<String> columns = Json.getStringList(t, "columns");
        List<Row> rows;
        if (t.containsKey("file")) {
            if (!(t.get("file") instanceof Map fm)) {
                throw new JoinException(JoinException.INVALID_REQUEST, "'file' must be an object");
            }
            Map<String, Object> fo = castMap(fm);
            Path path = Path.of(Json.getString(fo, "path"));
            String format = Json.optString(fo, "format", "jsonl");
            rows = readRowsFromFile(path, format, columns.size());
        } else {
            List<Object> rawRows = Json.getArray(t, "rows");
            rows = new ArrayList<>(rawRows.size());
            for (Object o : rawRows) rows.add(Row.ofRaw(asList(o)));
        }
        return new Table(name, new Schema(columns), rows);
    }

    private static List<Object> asList(Object o) {
        if (!(o instanceof List<?> l)) {
            throw new JoinException(JoinException.INVALID_REQUEST, "Each row must be an array");
        }
        return new ArrayList<>(l);
    }

    private static List<Row> readRowsFromFile(Path path, String format, int width) {
        if (!Files.exists(path)) {
            throw new JoinException(JoinException.INVALID_REQUEST, "Table file not found: " + path);
        }
        List<Row> rows = new ArrayList<>();
        try {
            if (format.equalsIgnoreCase("jsonl")) {
                for (String line : Files.readAllLines(path, StandardCharsets.UTF_8)) {
                    if (line.isBlank()) continue;
                    rows.add(Row.ofRaw(asList(Json.parse(line))));
                }
            } else if (format.equalsIgnoreCase("json")) {
                Object doc = Json.parse(Files.readString(path, StandardCharsets.UTF_8));
                if (doc instanceof Map<?, ?> m && m.containsKey("rows")) {
                    for (Object o : Json.getArray(castMap(m), "rows")) rows.add(Row.ofRaw(asList(o)));
                } else if (doc instanceof List<?> arr) {
                    for (Object o : arr) rows.add(Row.ofRaw(asList(o)));
                } else {
                    throw new JoinException(JoinException.INVALID_REQUEST,
                            "JSON table file must be an array or an object with 'rows'");
                }
            } else {
                throw new JoinException(JoinException.INVALID_REQUEST,
                        "file.format must be 'jsonl' or 'json'");
            }
        } catch (IOException e) {
            throw new JoinException(JoinException.IO_ERROR,
                    "Cannot read table file: " + path + " (" + e.getMessage() + ")", e);
        }
        for (Row r : rows) {
            if (r.size() != width) {
                throw new JoinException(JoinException.INVALID_REQUEST,
                        "Row width " + r.size() + " != declared " + width + " columns in " + path);
            }
        }
        return rows;
    }
}
