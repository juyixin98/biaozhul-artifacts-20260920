package tvl.api;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import tvl.analyzer.DataType;
import tvl.analyzer.Schema;
import tvl.analyzer.TypeChecker;
import tvl.analyzer.TypeException;
import tvl.engine.Catalog;
import tvl.engine.EngineException;
import tvl.engine.QueryEngine;
import tvl.engine.QueryResult;
import tvl.engine.QuerySpec;
import tvl.engine.Row;
import tvl.engine.Table;
import tvl.evaluator.EvalException;
import tvl.expr.Expr;
import tvl.expr.ExpressionException;
import tvl.json.JsonException;
import tvl.lexer.LexException;
import tvl.parser.ParseException;
import tvl.parser.Parser;

/**
 * JSON 请求入口（核心）：纯函数式分发器，与 HTTP/CLI 传输方式无关。
 *
 * 支持三种 action：
 * <ul>
 *   <li>{@code load}   —— 装载/替换一张内存表（含严格类型校验）</li>
 *   <li>{@code query}  —— 解析 + 类型检查 + 执行表达式查询，返回行、统计与执行计划</li>
 *   <li>{@code export} —— 导出全部数据（以及表结构），用于持久化</li>
 * </ul>
 *
 * 输入/输出均为可 JSON 序列化的 Java 标准类型（Map/List/String/Long/Boolean）。
 */
public final class JsonApi {

    private final Catalog catalog;
    private final QueryEngine engine;

    public JsonApi() {
        this(new Catalog());
    }

    public JsonApi(Catalog catalog) {
        this.catalog = catalog;
        this.engine = new QueryEngine(catalog);
    }

    public Catalog catalog() {
        return catalog;
    }

    /** 处理一个已解析的 JSON 请求对象。 */
    public Map<String, Object> handle(Map<String, Object> request) {
        try {
            Object actionValue = request.get("action");
            if (!(actionValue instanceof String action)) {
                return error("INVALID_REQUEST", "missing or invalid 'action' field", null);
            }
            return switch (action) {
                case "load" -> handleLoad(request);
                case "query" -> handleQuery(request);
                case "export" -> handleExport(request);
                default -> error("INVALID_REQUEST",
                        "unknown action '" + action + "' (expected load|query|export)", null);
            };
        } catch (ApiException ex) {
            return error(ex.code, ex.getMessage(), ex.detail);
        } catch (ExpressionException ex) {
            // 词法/语法/类型/求值错误理论上已在各自处理方法中包装，这里兜底
            return error("EXPRESSION_ERROR", ex.getMessage(), null);
        } catch (EngineException ex) {
            return error("ENGINE_ERROR", ex.getMessage(), null);
        }
    }

    /** 处理原始 JSON 字符串。 */
    public String handleJson(String json) {
        try {
            Object parsed = tvl.json.Json.parse(json);
            if (!(parsed instanceof Map<?, ?> rawMap)) {
                return tvl.json.Json.write(error("INVALID_REQUEST",
                        "request body must be a JSON object", null));
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> request = (Map<String, Object>) rawMap;
            return tvl.json.Json.write(handle(request));
        } catch (JsonException ex) {
            return tvl.json.Json.write(error("INVALID_JSON",
                    ex.getMessage() + " (offset " + ex.position + ")", null));
        }
    }

    // ---------------------------------------------------------------------
    // load
    // ---------------------------------------------------------------------

    private Map<String, Object> handleLoad(Map<String, Object> request) {
        String tableName = requireString(request, "table");
        if (tableName.isBlank()) {
            throw new ApiException("INVALID_REQUEST", "'table' must be a non-empty string", null);
        }
        List<?> columnsRaw = requireList(request, "columns");
        List<?> rowsRaw = optList(request, "rows");

        Table table = new Table(tableName);
        List<String> columnNames = new ArrayList<>();
        for (int i = 0; i < columnsRaw.size(); i++) {
            Object c = columnsRaw.get(i);
            if (!(c instanceof Map<?, ?> cm)) {
                throw new ApiException("INVALID_REQUEST",
                        "columns[" + i + "] must be an object {name,type}", null);
            }
            Object name = cm.get("name");
            Object type = cm.get("type");
            if (!(name instanceof String colName) || colName.isBlank()) {
                throw new ApiException("INVALID_REQUEST",
                        "columns[" + i + "].name must be a non-empty string", null);
            }
            if (!(type instanceof String typeName)) {
                throw new ApiException("INVALID_REQUEST",
                        "columns[" + i + "].type must be a string", null);
            }
            DataType dataType = parseTypeName(typeName);
            if (dataType == null) {
                throw new ApiException("INVALID_REQUEST",
                        "columns[" + i + "].type '" + typeName
                                + "' is not supported (use INTEGER|STRING|BOOLEAN)", null);
            }
            if (columnNames.contains(colName)) {
                throw new ApiException("INVALID_REQUEST",
                        "duplicate column '" + colName + "'", null);
            }
            columnNames.add(colName);
            table.addColumn(colName, dataType);
        }
        if (columnNames.isEmpty()) {
            throw new ApiException("INVALID_REQUEST", "table must define at least one column", null);
        }

        List<Row> parsedRows = new ArrayList<>();
        for (int ri = 0; ri < rowsRaw.size(); ri++) {
            Object ro = rowsRaw.get(ri);
            if (!(ro instanceof Map<?, ?> rm)) {
                throw new ApiException("TYPE_MISMATCH",
                        "rows[" + ri + "] must be an object", null);
            }
            Row row = new Row();
            for (String col : columnNames) {
                if (!rm.containsKey(col)) {
                    throw new ApiException("TYPE_MISMATCH",
                            "rows[" + ri + "] is missing column '" + col + "'", null);
                }
                Object value = rm.get(col);
                DataType expected = table.typeOf(col);
                row.set(col, coerceValue(value, expected, "rows[" + ri + "]." + col));
            }
            for (Object extraKey : rm.keySet()) {
                if (!columnNames.contains(extraKey)) {
                    throw new ApiException("TYPE_MISMATCH",
                            "rows[" + ri + "] has unknown column '" + extraKey + "'", null);
                }
            }
            parsedRows.add(row);
        }
        for (Row r : parsedRows) {
            table.addRow(r);
        }

        catalog.put(table);

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("ok", true);
        result.put("action", "load");
        result.put("table", tableName);
        result.put("columnsLoaded", columnNames.size());
        result.put("rowsLoaded", table.rowCount());
        return result;
    }

    private Object coerceValue(Object value, DataType expected, String path) {
        if (value == null) {
            return null; // SQL NULL
        }
        switch (expected) {
            case INTEGER:
                if (value instanceof Long l) return l;
                if (value instanceof Integer i) return i.longValue();
                throw new ApiException("TYPE_MISMATCH",
                        path + " expects INTEGER but got " + jsonKind(value), null);
            case STRING:
                if (value instanceof String s) return s;
                throw new ApiException("TYPE_MISMATCH",
                        path + " expects STRING but got " + jsonKind(value), null);
            case BOOLEAN:
                if (value instanceof Boolean b) return b;
                throw new ApiException("TYPE_MISMATCH",
                        path + " expects BOOLEAN but got " + jsonKind(value), null);
            default:
                throw new ApiException("TYPE_MISMATCH",
                        path + " has unsupported declared type " + expected.displayName(), null);
        }
    }

    private static String jsonKind(Object value) {
        if (value instanceof Long || value instanceof Integer) return "INTEGER";
        if (value instanceof String) return "STRING";
        if (value instanceof Boolean) return "BOOLEAN";
        if (value instanceof Double) return "FLOAT (only integer literals are supported)";
        return value.getClass().getSimpleName();
    }

    private static DataType parseTypeName(String name) {
        return switch (name.trim().toUpperCase()) {
            case "INTEGER", "INT", "BIGINT" -> DataType.INTEGER;
            case "STRING", "TEXT", "VARCHAR" -> DataType.STRING;
            case "BOOLEAN", "BOOL" -> DataType.BOOLEAN;
            default -> null;
        };
    }

    // ---------------------------------------------------------------------
    // query
    // ---------------------------------------------------------------------

    private Map<String, Object> handleQuery(Map<String, Object> request) {
        String tableName = requireString(request, "table");
        Table table = catalog.get(tableName);
        if (table == null) {
            throw new ApiException("UNKNOWN_TABLE", "unknown table '" + tableName + "'", null);
        }

        String whereText = optString(request, "where");
        Expr filter = null;
        if (whereText != null && !whereText.isBlank()) {
            filter = compileExpression(whereText, table);
        }

        List<String> select = null;
        if (request.get("select") != null) {
            List<?> selectRaw = requireList(request, "select");
            select = new ArrayList<>();
            for (int i = 0; i < selectRaw.size(); i++) {
                Object o = selectRaw.get(i);
                if (!(o instanceof String s)) {
                    throw new ApiException("INVALID_REQUEST",
                            "select[" + i + "] must be a column name string", null);
                }
                select.add(s);
            }
        }

        Long limit = null;
        Object limitValue = request.get("limit");
        if (limitValue != null) {
            if (!(limitValue instanceof Long lim)) {
                throw new ApiException("INVALID_REQUEST", "'limit' must be an integer", null);
            }
            if (lim < 0) {
                throw new ApiException("INVALID_REQUEST", "'limit' must be non-negative", null);
            }
            limit = lim;
        }

        QuerySpec spec = new QuerySpec(tableName, filter, whereText, select, limit);

        QueryResult result;
        try {
            result = engine.execute(spec);
        } catch (EvalException ex) {
            throw expressionError(ex, whereText);
        }
        return result.toJson();
    }

    /** 词法 -> 语法 -> 类型检查，任何阶段的错误都带位置信息。 */
    private Expr compileExpression(String text, Table table) {
        Expr expr;
        try {
            expr = Parser.parse(text);
        } catch (LexException ex) {
            throw expressionError(ex, text);
        } catch (ParseException ex) {
            throw expressionError(ex, text);
        }
        Schema schema = new Schema();
        table.columns().forEach(schema::add);
        try {
            new TypeChecker(schema).check(expr);
        } catch (TypeException ex) {
            throw expressionError(ex, text);
        }
        return expr;
    }

    private static ApiException expressionError(ExpressionException ex, String source) {
        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("message", ex.getMessage());
        detail.put("offset", ex.getPosition());
        if (source != null) {
            int[] lc = ErrorFormatter.lineAndColumn(source, ex.getPosition());
            detail.put("line", lc[0]);
            detail.put("column", lc[1]);
            detail.put("snippet", ErrorFormatter.caret(source, ex.getPosition()));
        }
        String code = switch (ex) {
            case LexException ignored -> "LEX_ERROR";
            case ParseException ignored -> "PARSE_ERROR";
            case TypeException ignored -> "TYPE_ERROR";
            case EvalException ignored -> "EVAL_ERROR";
            default -> "EXPRESSION_ERROR";
        };
        return new ApiException(code, ex.getMessage(), detail);
    }

    // ---------------------------------------------------------------------
    // export
    // ---------------------------------------------------------------------

    private Map<String, Object> handleExport(Map<String, Object> request) {
        String tableName = optString(request, "table");

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("ok", true);
        result.put("action", "export");

        List<Table> targets = new ArrayList<>();
        if (tableName != null) {
            Table table = catalog.get(tableName);
            if (table == null) {
                throw new ApiException("UNKNOWN_TABLE", "unknown table '" + tableName + "'", null);
            }
            targets.add(table);
        } else {
            targets.addAll(catalog.all().values());
        }

        List<Object> tablesJson = new ArrayList<>();
        for (Table table : targets) {
            tablesJson.add(exportTable(table));
        }
        result.put("tables", tablesJson);
        return result;
    }

    private Map<String, Object> exportTable(Table table) {
        Map<String, Object> tj = new LinkedHashMap<>();
        tj.put("name", table.name());

        List<Object> columns = new ArrayList<>();
        table.columns().forEach((name, type) -> {
            Map<String, Object> c = new LinkedHashMap<>();
            c.put("name", name);
            c.put("type", type.displayName());
            columns.add(c);
        });
        tj.put("columns", columns);

        List<Object> rows = new ArrayList<>();
        for (Row row : table.rows()) {
            Map<String, Object> r = new LinkedHashMap<>();
            for (String col : table.columns().keySet()) {
                r.put(col, row.get(col));
            }
            rows.add(r);
        }
        tj.put("rows", rows);
        tj.put("rowCount", table.rowCount());
        return tj;
    }

    // ---------------------------------------------------------------------
    // 请求字段辅助
    // ---------------------------------------------------------------------

    private static String requireString(Map<String, Object> request, String field) {
        Object value = request.get(field);
        if (!(value instanceof String s)) {
            throw new ApiException("INVALID_REQUEST",
                    "missing or invalid '" + field + "' field (string required)", null);
        }
        return s;
    }

    private static String optString(Map<String, Object> request, String field) {
        Object value = request.get(field);
        if (value == null) return null;
        if (!(value instanceof String s)) {
            throw new ApiException("INVALID_REQUEST", "'" + field + "' must be a string", null);
        }
        return s;
    }

    private static List<?> requireList(Map<String, Object> request, String field) {
        Object value = request.get(field);
        if (!(value instanceof List<?> list)) {
            throw new ApiException("INVALID_REQUEST",
                    "missing or invalid '" + field + "' field (array required)", null);
        }
        return list;
    }

    private static List<?> optList(Map<String, Object> request, String field) {
        Object value = request.get(field);
        if (value == null) return List.of();
        if (!(value instanceof List<?> list)) {
            throw new ApiException("INVALID_REQUEST", "'" + field + "' must be an array", null);
        }
        return list;
    }

    private static Map<String, Object> error(String code, String message, Map<String, Object> detail) {
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("ok", false);
        err.put("error", code);
        err.put("message", message);
        if (detail != null) {
            err.put("detail", detail);
        }
        return err;
    }

    /** 内部异常：携带错误码与可选定位详情。 */
    private static final class ApiException extends RuntimeException {
        final String code;
        final Map<String, Object> detail;

        ApiException(String code, String message, Map<String, Object> detail) {
            super(message);
            this.code = code;
            this.detail = detail;
        }
    }
}
