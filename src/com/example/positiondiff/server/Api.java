package com.example.positiondiff.server;

import com.example.positiondiff.diff.Budget;
import com.example.positiondiff.diff.DiffResult;
import com.example.positiondiff.diff.EditOp;
import com.example.positiondiff.json.Json;
import com.example.positiondiff.model.Eol;
import com.example.positiondiff.model.Hunk;
import com.example.positiondiff.model.Line;
import com.example.positiondiff.search.SearchIndex;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Converts between JSON request/response maps and the diff/search domain. */
public final class Api {

    private Api() {}

    // ---- input decoding ----

    public static String decodeText(Map<String, Object> req, String field) {
        Object v = req.get(field);
        if (v == null) {
            throw new IllegalArgumentException("missing required field '" + field + "'");
        }
        if (v instanceof String s) return s;
        if (v instanceof List<?> list) {
            StringBuilder sb = new StringBuilder();
            for (Object item : list) {
                Map<?, ?> lineObj = asLineObject(item);
                Object textValue = lineObj.get("text");
                String text = textValue == null ? "" : String.valueOf(textValue);
                Object eol = lineObj.get("eol");
                sb.append(text).append(decodeEol(eol).separator());
            }
            return sb.toString();
        }
        throw new IllegalArgumentException("field '" + field + "' must be a string or an array");
    }

    @SuppressWarnings("unchecked")
    private static Map<?, ?> asLineObject(Object item) {
        if (item instanceof Map<?, ?> m) return m;
        throw new IllegalArgumentException("line entries must be objects with 'text' and 'eol'");
    }

    private static Eol decodeEol(Object eol) {
        if (eol == null) return Eol.NONE;
        return switch (String.valueOf(eol)) {
            case "LF", "lf", "\n" -> Eol.LF;
            case "CRLF", "crlf", "\r\n" -> Eol.CRLF;
            case "NONE", "none", "" -> Eol.NONE;
            default -> throw new IllegalArgumentException("unknown eol: " + eol);
        };
    }

    public static Budget decodeBudget(Map<String, Object> req) {
        Object b = req.get("budget");
        if (b == null) return Budget.UNLIMITED_BUDGET;
        if (!(b instanceof Map<?, ?> m)) {
            throw new IllegalArgumentException("'budget' must be an object");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> budgetMap = (Map<String, Object>) m;
        long maxNodes = Json.getLong(budgetMap, "maxNodes", Budget.UNLIMITED);
        long maxD = Json.getLong(budgetMap, "maxD", Budget.UNLIMITED);
        return new Budget(maxNodes, maxD);
    }

    @SuppressWarnings("unchecked")
    public static List<EditOp> decodeOps(List<Object> opsInput, List<Line> oldLines) {
        List<EditOp> ops = new ArrayList<>();
        for (Object o : opsInput) {
            if (!(o instanceof Map<?, ?> m)) {
                throw new IllegalArgumentException("each op must be an object");
            }
            String type = String.valueOf(m.get("type"));
            int oldIndex = intVal(m.get("oldIndex"), "oldIndex");
            int newIndex = intVal(m.get("newIndex"), "newIndex");
            EditOp op = switch (type) {
                case "equal" -> {
                    Line line = decodeOpLine(m, oldLines, oldIndex);
                    yield EditOp.equal(line, oldIndex, newIndex);
                }
                case "delete" -> {
                    Line line = decodeOpLine(m, oldLines, oldIndex);
                    yield EditOp.delete(line, oldIndex, newIndex);
                }
                case "insert" -> EditOp.insert(decodeInsertLine(m), oldIndex, newIndex);
                default -> throw new IllegalArgumentException("unknown op type: " + type);
            };
            ops.add(op);
        }
        return ops;
    }

    private static int intVal(Object v, String field) {
        if (v instanceof Number n) {
            if (n.doubleValue() != Math.floor(n.doubleValue())) {
                throw new IllegalArgumentException(field + " must be an integer");
            }
            return n.intValue();
        }
        throw new IllegalArgumentException("missing integer field '" + field + "'");
    }

    private static Line decodeOpLine(Map<?, ?> m, List<Line> oldLines, int oldIndex) {
        Object lineObj = m.get("line");
        if (lineObj != null) return decodeInsertLine(m);
        if (oldIndex < 0 || oldIndex >= oldLines.size()) {
            throw new IllegalArgumentException("oldIndex out of range: " + oldIndex);
        }
        return oldLines.get(oldIndex);
    }

    private static Line decodeInsertLine(Map<?, ?> m) {
        Object lineObj = m.get("line");
        if (!(lineObj instanceof Map<?, ?> lm)) {
            throw new IllegalArgumentException("op 'line' must be {text, eol}");
        }
        Object textValue = lm.get("text");
        String text = textValue == null ? "" : String.valueOf(textValue);
        return new Line(text, decodeEol(lm.get("eol")));
    }

    // ---- output encoding ----

    public static Map<String, Object> encodeResult(DiffResult result,
                                            List<Line> oldLines, List<Line> newLines,
                                            int context) {
        Map<String, Object> out = Json.obj();
        out.put("optimal", result.optimal());
        out.put("degraded", !result.optimal());
        if (result.degradedReason() != null) {
            out.put("degradedReason", result.degradedReason());
        }
        out.put("shortest", result.optimal());
        Map<String, Object> stats = Json.obj();
        stats.put("editDistance", result.editDistance());
        stats.put("shortestDistance", result.shortestDistance());
        stats.put("insertions", countType(result.ops(), EditOp.Type.INSERT));
        stats.put("deletions", countType(result.ops(), EditOp.Type.DELETE));
        stats.put("equals", countType(result.ops(), EditOp.Type.EQUAL));
        stats.put("nodesUsed", result.nodesUsed());
        stats.put("elapsedMicros", Math.round(result.elapsedNanos() / 1000.0));
        stats.put("oldLineCount", oldLines.size());
        stats.put("newLineCount", newLines.size());
        out.put("stats", stats);

        out.put("ops", encodeOps(result.ops()));

        List<Hunk> hunks = context == 3 ? result.hunks()
                : com.example.positiondiff.diff.Hunks.build(
                        result.ops(), oldLines.size(), newLines.size(), context);
        out.put("hunks", encodeHunks(hunks));
        return out;
    }

    private static int countType(List<EditOp> ops, EditOp.Type type) {
        int c = 0;
        for (EditOp op : ops) if (op.type() == type) c++;
        return c;
    }

    public static List<Object> encodeOps(List<EditOp> ops) {
        List<Object> arr = Json.arr();
        for (EditOp op : ops) {
            Map<String, Object> m = Json.obj();
            m.put("type", switch (op.type()) {
                case EQUAL -> "equal";
                case DELETE -> "delete";
                case INSERT -> "insert";
            });
            m.put("oldIndex", op.oldIndex());
            m.put("newIndex", op.newIndex());
            m.put("line", encodeLine(op.line()));
            arr.add(m);
        }
        return arr;
    }

    public static Map<String, Object> encodeLine(Line line) {
        Map<String, Object> m = Json.obj();
        m.put("text", line.text());
        m.put("eol", line.eol().name());
        return m;
    }

    public static List<Object> encodeHunks(List<Hunk> hunks) {
        List<Object> arr = Json.arr();
        for (Hunk h : hunks) {
            Map<String, Object> m = Json.obj();
            Map<String, Object> oldRange = Json.obj();
            oldRange.put("start", h.oldStart());
            oldRange.put("count", h.oldCount());
            Map<String, Object> newRange = Json.obj();
            newRange.put("start", h.newStart());
            newRange.put("count", h.newCount());
            m.put("oldRange", oldRange);
            m.put("newRange", newRange);
            m.put("ops", encodeOps(h.ops()));
            arr.add(m);
        }
        return arr;
    }

    public static Map<String, Object> encodeSearch(SearchIndex.Response response) {
        Map<String, Object> out = Json.obj();
        out.put("query", response.query());
        out.put("queryTerms", response.queryTerms());
        List<Object> hits = Json.arr();
        for (SearchIndex.Hit h : response.hits()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("docId", h.docId());
            m.put("path", h.path());
            m.put("title", h.title());
            m.put("score", h.score());
            m.put("matchedTerms", h.matchedTerms());
            m.put("totalTerms", h.totalTerms());
            m.put("lineNumber", h.lineNumber());
            m.put("line", h.line());
            hits.add(m);
        }
        out.put("hits", hits);
        return out;
    }
}
