package engine.executor;

import engine.json.Json;
import engine.json.JsonException;
import engine.json.JsonWriter;
import engine.model.ColumnType;
import engine.model.Relation;
import engine.model.Relation.Row;
import engine.window.BoundKind;
import engine.window.Frame;
import engine.window.FrameBound;
import engine.window.FunctionSpec;
import engine.window.NullOrder;
import engine.window.OrderKey;
import engine.window.WindowException;
import engine.window.WindowEngine;
import engine.window.WindowFunction;
import engine.window.WindowSpec;

import java.util.ArrayList;
import java.util.List;

/**
 * 请求执行器：把 JSON 请求解析为 {@link Relation} + {@link WindowSpec}，
 * 调用 {@link WindowEngine}，并组装可导出的 JSON 响应（含执行计划）。
 *
 * <p>所有面向请求方的错误（JSON 语法错误、结构错误、窗口计算溢出）
 * 都会被 {@link #execute(String)} 捕获并转为 {@code ok:false} 响应，
 * 退出码由 {@code Main} 区分。
 */
public final class RequestExecutor {

    /** 逻辑错误：请求本身有问题（区别于溢出类窗口错误，用于退出码分类）。 */
    public static final class RequestException extends RuntimeException {
        public RequestException(String message) {
            super(message);
        }
    }

    public boolean lastRequestFailed = false;
    public boolean lastFailureWasWindowError = false;

    public String execute(String requestText) {
        lastRequestFailed = false;
        lastFailureWasWindowError = false;
        try {
            Json root = JsonParserParse(requestText);
            if (!(root instanceof Json.JObj req)) {
                throw new RequestException("请求顶层必须是 JSON 对象");
            }
            return run(req);
        } catch (JsonException e) {
            return fail("JSON_PARSE_ERROR", e.getMessage());
        } catch (RequestException | IllegalArgumentException e) {
            return fail("INVALID_REQUEST", e.getMessage());
        } catch (WindowException e) {
            lastFailureWasWindowError = true;
            return fail("WINDOW_ERROR", e.getMessage());
        }
    }

    // 隔离静态导入风格，保持调用点可读
    private static Json JsonParserParse(String text) {
        return engine.json.JsonParser.parse(text);
    }

    private String run(Json.JObj req) {
        boolean includePlan = optBool(req, "includePlan", false);
        boolean includeData = optBool(req, "includeData", true);

        Json inputNode = field(req, "input");
        if (!(inputNode instanceof Json.JObj)) {
            throw new RequestException("'input' 必须是对象");
        }
        Relation input = parseRelation((Json.JObj) inputNode);

        Json windowNode = field(req, "window");
        if (!(windowNode instanceof Json.JObj)) {
            throw new RequestException("'window' 必须是对象");
        }
        WindowSpec spec = parseWindow((Json.JObj) windowNode, input);

        Json.JObj plan = includePlan ? buildPlan(input, spec) : null;

        Json.JObj resp = JsonWriter.obj("ok", Boolean.TRUE);
        if (includePlan) {
            resp.put("plan", plan);
        }
        if (includeData) {
            WindowEngine eng = new WindowEngine();
            Relation output = eng.execute(input, spec);
            resp.put("output", relationToJson(output));
        }
        return JsonWriter.write(resp, true);
    }

    private String fail(String type, String message) {
        lastRequestFailed = true;
        return JsonWriter.write(
                JsonWriter.obj(
                        "ok", Boolean.FALSE,
                        "error", JsonWriter.obj("type", type, "message", message)),
                true);
    }

    // ------------------------------------------------------------------
    // 输入解析
    // ------------------------------------------------------------------

    private Relation parseRelation(Json.JObj node) {
        Json colsNode = field(node, "columns");
        Json rowsNode = field(node, "rows");
        if (!(colsNode instanceof Json.JArr colsArr)) {
            throw new RequestException("'input.columns' 必须是数组");
        }
        if (!(rowsNode instanceof Json.JArr rowsArr)) {
            throw new RequestException("'input.rows' 必须是数组（允许空数组）");
        }

        List<String> names = new ArrayList<>();
        List<ColumnType> types = new ArrayList<>();
        for (Json c : colsArr.items) {
            if (!(c instanceof Json.JObj co)) {
                throw new RequestException("'input.columns' 的每个元素必须是对象");
            }
            Json nameNode = field(co, "name");
            Json typeNode = field(co, "type");
            if (!(nameNode instanceof Json.JStr n)) {
                throw new RequestException("列名必须是字符串");
            }
            if (names.contains(n.value())) {
                throw new RequestException("列名重复: " + n.value());
            }
            if (!(typeNode instanceof Json.JStr t)) {
                throw new RequestException("列类型必须是字符串（LONG 或 STRING）");
            }
            ColumnType type = switch (t.value()) {
                case "LONG" -> ColumnType.LONG;
                case "STRING" -> ColumnType.STRING;
                default -> throw new RequestException("不支持的列类型: " + t.value()
                        + "（仅支持 LONG、STRING）");
            };
            names.add(n.value());
            types.add(type);
        }
        if (names.isEmpty()) {
            throw new RequestException("'input.columns' 至少需要一列");
        }

        List<Row> rows = new ArrayList<>(rowsArr.items.size());
        for (int r = 0; r < rowsArr.items.size(); r++) {
            Json rowNode = rowsArr.items.get(r);
            if (!(rowNode instanceof Json.JArr rowArr)) {
                throw new RequestException("第 " + (r + 1) + " 行必须是数组");
            }
            if (rowArr.items.size() != names.size()) {
                throw new RequestException("第 " + (r + 1) + " 行有 "
                        + rowArr.items.size() + " 个值，与列数 " + names.size() + " 不一致");
            }
            Object[] values = new Object[names.size()];
            for (int c = 0; c < names.size(); c++) {
                values[c] = convertCell(rowArr.items.get(c), names.get(c), types.get(c), r, c);
            }
            rows.add(new Row(r, values));
        }
        return new Relation(names, types, rows);
    }

    private Object convertCell(Json v, String column, ColumnType type, int row, int col) {
        if (v instanceof Json.JNull) {
            return null;
        }
        if (type == ColumnType.LONG) {
            if (v instanceof Json.JLong n) {
                return n.value();
            }
            throw new RequestException(String.format(
                    "第 %d 行列 '%s' 声明为 LONG，但值不是整数（聚合只支持可检查溢出的整数）",
                    row + 1, column));
        }
        if (v instanceof Json.JStr s) {
            return s.value();
        }
        throw new RequestException(String.format(
                "第 %d 行列 '%s' 声明为 STRING，但值不是字符串", row + 1, column));
    }

    // ------------------------------------------------------------------
    // 窗口规格解析
    // ------------------------------------------------------------------

    private WindowSpec parseWindow(Json.JObj node, Relation input) {
        List<String> partitionBy = parseStringList(node.get("partitionBy"), "window.partitionBy");
        List<OrderKey> orderBy = new ArrayList<>();
        if (node.get("orderBy") instanceof Json.JArr arr) {
            for (Json item : arr.items) {
                if (!(item instanceof Json.JObj o)) {
                    throw new RequestException("'window.orderBy' 的元素必须是对象");
                }
                Json colNode = field(o, "column");
                if (!(colNode instanceof Json.JStr col)) {
                    throw new RequestException("排序键的 'column' 必须是字符串");
                }
                boolean ascending = optBool(o, "ascending", true);
                // SQL/PostgreSQL 默认：ASC → NULLS LAST，DESC → NULLS FIRST
                NullOrder defaultNulls = ascending ? NullOrder.NULLS_LAST : NullOrder.NULLS_FIRST;
                NullOrder nullOrder = parseNullOrder(o.get("nullOrder"), defaultNulls);
                orderBy.add(new OrderKey(col.value(), ascending, nullOrder));
            }
        } else if (node.get("orderBy") != null) {
            throw new RequestException("'window.orderBy' 必须是数组");
        }

        Json fnsNode = field(node, "functions");
        if (!(fnsNode instanceof Json.JArr fnsArr) || fnsArr.items.isEmpty()) {
            throw new RequestException("'window.functions' 必须是非空数组");
        }
        List<FunctionSpec> functions = new ArrayList<>();
        for (Json fn : fnsArr.items) {
            if (!(fn instanceof Json.JObj fo)) {
                throw new RequestException("函数规格必须是对象");
            }
            functions.add(parseFunction(fo));
        }
        return new WindowSpec(partitionBy, orderBy, functions);
    }

    private FunctionSpec parseFunction(Json.JObj fo) {
        Json typeNode = field(fo, "type");
        if (!(typeNode instanceof Json.JStr typeStr)) {
            throw new RequestException("函数的 'type' 必须是字符串");
        }
        Json outNode = field(fo, "outputColumn");
        if (!(outNode instanceof Json.JStr outStr)) {
            throw new RequestException("函数的 'outputColumn' 必须是字符串");
        }
        String out = outStr.value();
        WindowFunction type = switch (typeStr.value()) {
            case "ROW_NUMBER" -> WindowFunction.ROW_NUMBER;
            case "RANK" -> WindowFunction.RANK;
            case "SUM" -> WindowFunction.SUM;
            default -> throw new RequestException("不支持的窗口函数: " + typeStr.value()
                    + "（支持 ROW_NUMBER、RANK、SUM）");
        };
        if (type == WindowFunction.SUM) {
            Json argNode = field(fo, "argument");
            if (!(argNode instanceof Json.JStr argStr)) {
                throw new RequestException("SUM 的 'argument' 必须是列名字符串");
            }
            Frame frame = fo.get("frame") == null
                    ? Frame.defaultForSum()
                    : parseFrame(fo.get("frame"));
            return FunctionSpec.sum(out, argStr.value(), frame);
        }
        return type == WindowFunction.ROW_NUMBER
                ? FunctionSpec.rowNumber(out)
                : FunctionSpec.rank(out);
    }

    private Frame parseFrame(Json node) {
        if (!(node instanceof Json.JObj fo)) {
            throw new RequestException("'frame' 必须是对象");
        }
        Json modeNode = fo.get("mode");
        if (modeNode instanceof Json.JStr mode && !mode.value().equals("ROWS")) {
            throw new RequestException("仅支持 ROWS 帧模式，实际为: " + mode.value());
        }
        Json startNode = field(fo, "start");
        Json endNode = field(fo, "end");
        FrameBound start = parseBound(startNode, "start");
        FrameBound end = parseBound(endNode, "end");
        try {
            return new Frame(start, end);
        } catch (IllegalArgumentException e) {
            throw new RequestException(e.getMessage());
        }
    }

    private FrameBound parseBound(Json node, String label) {
        if (!(node instanceof Json.JObj bo)) {
            throw new RequestException("帧边界 '" + label + "' 必须是对象");
        }
        if (!(field(bo, "kind") instanceof Json.JStr kindStr)) {
            throw new RequestException("帧边界 '" + label + "' 的 'kind' 必须是字符串");
        }
        BoundKind kind = switch (kindStr.value()) {
            case "UNBOUNDED_PRECEDING" -> BoundKind.UNBOUNDED_PRECEDING;
            case "PRECEDING" -> BoundKind.PRECEDING;
            case "CURRENT_ROW" -> BoundKind.CURRENT_ROW;
            case "FOLLOWING" -> BoundKind.FOLLOWING;
            case "UNBOUNDED_FOLLOWING" -> BoundKind.UNBOUNDED_FOLLOWING;
            default -> throw new RequestException("未知帧边界类型: " + kindStr.value());
        };
        if (kind == BoundKind.PRECEDING || kind == BoundKind.FOLLOWING) {
            Json offNode = field(bo, "offset");
            if (!(offNode instanceof Json.JLong off) || off.value() < 0) {
                throw new RequestException("帧边界 '" + label + "' 的 'offset' 必须是非负整数");
            }
            if (off.value() == 0) {
                return FrameBound.currentRow(); // 0 PRECEDING / 0 FOLLOWING 归一化
            }
            return new FrameBound(kind, off.value());
        }
        if (bo.get("offset") != null) {
            throw new RequestException("帧边界 '" + label + "' 只有 PRECEDING/FOLLOWING 允许 offset");
        }
        return new FrameBound(kind, 0);
    }

    private NullOrder parseNullOrder(Json node, NullOrder dflt) {
        if (node == null) {
            return dflt;
        }
        if (!(node instanceof Json.JStr s)) {
            throw new RequestException("'nullOrder' 必须是 NULLS_FIRST 或 NULLS_LAST");
        }
        return switch (s.value()) {
            case "NULLS_FIRST" -> NullOrder.NULLS_FIRST;
            case "NULLS_LAST" -> NullOrder.NULLS_LAST;
            default -> throw new RequestException("未知 nullOrder: " + s.value());
        };
    }

    private List<String> parseStringList(Json node, String label) {
        if (node == null) {
            return List.of();
        }
        if (!(node instanceof Json.JArr arr)) {
            throw new RequestException("'" + label + "' 必须是字符串数组");
        }
        List<String> result = new ArrayList<>();
        for (Json item : arr.items) {
            if (!(item instanceof Json.JStr s)) {
                throw new RequestException("'" + label + "' 的元素必须是字符串");
            }
            result.add(s.value());
        }
        return result;
    }

    /** 取必需字段；缺失属于请求结构错误（INVALID_REQUEST），不是 JSON 语法错误。 */
    private static Json field(Json.JObj o, String key) {
        Json v = o.get(key);
        if (v == null) {
            throw new RequestException("缺少必需字段 '" + key + "'");
        }
        return v;
    }

    private boolean optBool(Json.JObj o, String key, boolean dflt) {
        Json v = o.get(key);
        if (v == null) {
            return dflt;
        }
        if (v instanceof Json.JBool b) {
            return b.value();
        }
        throw new RequestException("'" + key + "' 必须是布尔值");
    }

    // ------------------------------------------------------------------
    // 计划与结果导出
    // ------------------------------------------------------------------

    private Json.JObj buildPlan(Relation input, WindowSpec spec) {
        Json.JArr cols = new Json.JArr();
        for (int i = 0; i < input.columnCount(); i++) {
            cols.add(JsonWriter.obj(
                    "name", input.columnNames().get(i),
                    "type", input.columnTypes().get(i).name()));
        }
        Json.JArr order = new Json.JArr();
        for (OrderKey k : spec.orderBy()) {
            order.add(JsonWriter.obj(
                    "column", k.column(),
                    "ascending", k.ascending(),
                    "nullOrder", k.nullOrder().name()));
        }
        Json.JArr fns = new Json.JArr();
        for (FunctionSpec f : spec.functions()) {
            Json.JObj fo = JsonWriter.obj(
                    "function", f.function().name(),
                    "sql", f.toSql(),
                    "outputColumn", f.outputColumn());
            if (f.argumentColumn() != null) {
                fo.put("argument", JsonWriter.of(f.argumentColumn()));
            }
            fns.add(fo);
        }
        Json.JArr partKeys = new Json.JArr();
        for (String p : spec.partitionBy()) {
            partKeys.add(JsonWriter.of(p));
        }
        return JsonWriter.obj(
                "engine", "in-memory-window-engine",
                "inputColumns", cols,
                "partitionBy", partKeys,
                "orderBy", order,
                "functions", fns);
    }

    private Json.JObj relationToJson(Relation r) {
        Json.JArr cols = new Json.JArr();
        for (int i = 0; i < r.columnCount(); i++) {
            cols.add(JsonWriter.obj(
                    "name", r.columnNames().get(i),
                    "type", r.columnTypes().get(i).name()));
        }
        Json.JArr rows = new Json.JArr();
        for (Row row : r.rows()) {
            Json.JArr arr = new Json.JArr();
            for (Object v : row.values) {
                arr.add(JsonWriter.of(v));
            }
            rows.add(arr);
        }
        return JsonWriter.obj(
                "columns", cols,
                "rowCount", (long) r.rows().size(),
                "rows", rows);
    }
}
