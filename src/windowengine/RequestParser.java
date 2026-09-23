package windowengine;

import windowengine.plan.Direction;
import windowengine.plan.Frame;
import windowengine.plan.FrameBound;
import windowengine.plan.FrameBoundType;
import windowengine.plan.FrameMode;
import windowengine.plan.FunctionCall;
import windowengine.plan.NullOrder;
import windowengine.plan.OrderKey;
import windowengine.plan.QueryPlan;
import windowengine.plan.WindowFunction;
import windowengine.plan.WindowSpec;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;

/**
 * 请求解析器：JSON 树 -&gt; {@link QueryRequest}。
 * 所有语义校验（列存在性、重名、帧合法性、类型等）在解析阶段完成，
 * 执行引擎可以假定计划合法。
 *
 * 请求顶层结构：
 * {
 *   "data": { "columns": [{"name","type"} | "colName"], "rows": [[...], ...] }
 *           或 { "file": "相对/绝对路径，内容同上" },
 *   "plan": {
 *     "window": { "partitionBy": [...], "orderBy": [{"column","direction","nullOrder"}],
 *                 "frame": "ROWS BETWEEN ... AND ..." | {mode,start,end} },
 *     "functions": [ {"function":"ROW_NUMBER|RANK|SUM","alias":"...","column":"...(SUM)"} ]
 *   },
 *   "export": "目录路径"  或  {"dir":"目录路径"}
 * }
 */
public final class RequestParser {

    private Schema schema;

    public QueryRequest parse(Object rootNode) {
        Map<String, Object> root = Json.asObject(rootNode, "请求根节点");

        Relation data = parseData(root.get("data"));
        this.schema = data.schema();

        Object planNode = root.get("plan");
        if (planNode == null) {
            throw new EngineException(ErrorCode.INVALID_REQUEST, "缺少 'plan' 字段");
        }
        QueryPlan plan = parsePlan(Json.asObject(planNode, "'plan'"));

        String exportDir = parseExport(root.get("export"));
        return new QueryRequest(data, plan, exportDir);
    }

    // ---------- 数据 ----------

    private Relation parseData(Object dataNode) {
        if (dataNode == null) {
            throw new EngineException(ErrorCode.INVALID_REQUEST, "缺少 'data' 字段");
        }
        if (dataNode instanceof Map<?, ?> dm && dm.containsKey("file")) {
            Object f = dm.get("file");
            if (!(f instanceof String filePath) || filePath.isBlank()) {
                throw new EngineException(ErrorCode.INVALID_REQUEST,
                        "data.file 必须是非空字符串路径");
            }
            Object loaded = Json.parseFile(Path.of(filePath));
            return buildRelation(Json.asObject(loaded, "数据文件根节点"));
        }
        return buildRelation(Json.asObject(dataNode, "'data'"));
    }

    private Relation buildRelation(Map<String, Object> dataObj) {
        Object colsNode = dataObj.get("columns");
        if (colsNode == null) {
            throw new EngineException(ErrorCode.INVALID_REQUEST, "data 缺少 'columns'");
        }
        List<Object> colNodes = Json.asArray(colsNode, "data.columns");
        if (colNodes.isEmpty()) {
            throw new EngineException(ErrorCode.INVALID_REQUEST, "data.columns 至少要有一列");
        }

        List<String> names = new ArrayList<>();
        List<Value.Type> types = new ArrayList<>();
        LinkedHashSet<String> seen = new LinkedHashSet<>();
        for (Object colNode : colNodes) {
            String name;
            Value.Type type = Value.Type.LONG;
            if (colNode instanceof String s) {
                name = s; // 简写：裸列名默认 LONG
            } else if (colNode instanceof Map<?, ?> cm) {
                Object nm = cm.get("name");
                if (!(nm instanceof String ns) || ns.isBlank()) {
                    throw new EngineException(ErrorCode.INVALID_REQUEST,
                            "列定义的 name 必须是非空字符串");
                }
                name = ns;
                Object tp = cm.get("type");
                if (tp != null) {
                    if (!(tp instanceof String ts)) {
                        throw new EngineException(ErrorCode.INVALID_REQUEST,
                                "列 " + name + " 的 type 必须是字符串");
                    }
                    type = switch (ts.toUpperCase()) {
                        case "LONG", "INT", "INTEGER", "BIGINT" -> Value.Type.LONG;
                        case "STRING", "TEXT", "VARCHAR" -> Value.Type.STRING;
                        default -> throw new EngineException(ErrorCode.UNSUPPORTED,
                                "列 " + name + " 使用了不支持的类型: " + ts
                                        + "（仅支持 LONG / STRING）");
                    };
                }
            } else {
                throw new EngineException(ErrorCode.INVALID_REQUEST,
                        "列定义必须是字符串或 {name,type} 对象");
            }
            if (!seen.add(name.toLowerCase())) {
                throw new EngineException(ErrorCode.DUPLICATE_COLUMN,
                        "输入列重名（大小写不敏感）: " + name);
            }
            names.add(name);
            types.add(type);
        }
        Schema sch = new Schema(names, types);

        Object rowsNode = dataObj.get("rows");
        if (rowsNode == null) {
            throw new EngineException(ErrorCode.INVALID_REQUEST, "data 缺少 'rows'");
        }
        List<Object> rowNodes = Json.asArray(rowsNode, "data.rows");
        List<Row> rows = new ArrayList<>(rowNodes.size());
        for (int r = 0; r < rowNodes.size(); r++) {
            List<Object> cells = Json.asArray(rowNodes.get(r), "第 " + r + " 行");
            if (cells.size() != sch.size()) {
                throw new EngineException(ErrorCode.INVALID_REQUEST,
                        "第 " + r + " 行列数为 " + cells.size()
                                + "，与 schema 的 " + sch.size() + " 列不符");
            }
            Value[] values = new Value[sch.size()];
            for (int c = 0; c < cells.size(); c++) {
                values[c] = convertCell(cells.get(c), sch.type(c), sch.name(c), r);
            }
            rows.add(new Row(values, r));
        }
        return new Relation(sch, rows);
    }

    private Value convertCell(Object raw, Value.Type declaredType, String colName, int rowIdx) {
        if (raw == null) {
            return Value.NULL;
        }
        if (raw instanceof Long l) {
            if (declaredType != Value.Type.LONG) {
                throw new EngineException(ErrorCode.TYPE_MISMATCH,
                        "第 " + rowIdx + " 行列 " + colName + " 声明为 STRING，却收到整数 " + l);
            }
            return Value.ofLong(l);
        }
        if (raw instanceof String s) {
            if (declaredType != Value.Type.STRING) {
                throw new EngineException(ErrorCode.TYPE_MISMATCH,
                        "第 " + rowIdx + " 行列 " + colName + " 声明为 LONG，却收到字符串 '" + s + "'");
            }
            return Value.ofString(s);
        }
        throw new EngineException(ErrorCode.INVALID_REQUEST,
                "第 " + rowIdx + " 行列 " + colName
                        + " 的值类型不支持（仅支持整数、字符串、null）");
    }

    // ---------- 计划 ----------

    private QueryPlan parsePlan(Map<String, Object> planObj) {
        WindowSpec window = parseWindow(planObj.get("window"));

        Object fnsNode = planObj.get("functions");
        if (fnsNode == null) {
            throw new EngineException(ErrorCode.INVALID_REQUEST, "plan 缺少 'functions'");
        }
        List<Object> fnNodes = Json.asArray(fnsNode, "plan.functions");
        if (fnNodes.isEmpty()) {
            throw new EngineException(ErrorCode.INVALID_REQUEST,
                    "plan.functions 至少要请求一个窗口函数");
        }

        List<FunctionCall> functions = new ArrayList<>();
        LinkedHashSet<String> aliases = new LinkedHashSet<>();
        for (Object fnNode : fnNodes) {
            Map<String, Object> fn = Json.asObject(fnNode, "functions 元素");
            String fnRaw = Json.requireString(fn, "function");
            WindowFunction wf = WindowFunction.parse(fnRaw);

            String alias = Json.optionalString(fn, "alias");
            if (alias == null || alias.isBlank()) {
                alias = defaultAlias(wf, fn.get("column"), functions.size());
            }
            if (schema.contains(alias)) {
                throw new EngineException(ErrorCode.DUPLICATE_COLUMN,
                        "窗口输出列与输入列重名: " + alias);
            }
            if (!aliases.add(alias.toLowerCase())) {
                throw new EngineException(ErrorCode.DUPLICATE_COLUMN,
                        "窗口输出列别名重复: " + alias);
            }

            String argument = null;
            if (wf == WindowFunction.SUM) {
                argument = Json.requireString(fn, "column");
                int argIdx = schema.requireIndex(argument);
                if (schema.type(argIdx) != Value.Type.LONG) {
                    throw new EngineException(ErrorCode.TYPE_MISMATCH,
                            "SUM 的参数列 " + argument + " 必须是 LONG 类型");
                }
            } else if (fn.containsKey("column")) {
                // ROW_NUMBER / RANK 不接受参数，提前给出明确错误而不是静默忽略
                throw new EngineException(ErrorCode.INVALID_REQUEST,
                        wf + " 不接受 column 参数（它是排名函数）");
            }
            functions.add(new FunctionCall(wf, alias, argument));
        }

        // 解析所有列引用，确保它们存在
        for (String p : window.partitionBy()) {
            schema.requireIndex(p);
        }
        for (OrderKey ok : window.orderBy()) {
            schema.requireIndex(ok.column());
        }

        return new QueryPlan("inline", window, functions);
    }

    private String defaultAlias(WindowFunction wf, Object columnNode, int index) {
        if (wf == WindowFunction.SUM && columnNode instanceof String s) {
            return "sum_" + s;
        }
        return wf.name().toLowerCase() + (index + 1);
    }

    private WindowSpec parseWindow(Object windowNode) {
        List<String> partitionBy = new ArrayList<>();
        List<OrderKey> orderBy = new ArrayList<>();
        Frame frame = null;

        if (windowNode != null) {
            Map<String, Object> win = Json.asObject(windowNode, "'window'");

            Object pb = win.get("partitionBy");
            if (pb != null) {
                for (Object p : Json.asArray(pb, "window.partitionBy")) {
                    if (!(p instanceof String s) || s.isBlank()) {
                        throw new EngineException(ErrorCode.INVALID_REQUEST,
                                "partitionBy 元素必须是非空列名字符串");
                    }
                    partitionBy.add(s);
                }
            }

            Object ob = win.get("orderBy");
            if (ob != null) {
                for (Object o : Json.asArray(ob, "window.orderBy")) {
                    orderBy.add(parseOrderKey(o));
                }
            }

            frame = parseFrame(win.get("frame"));
        }

        if (frame == null) {
            // SQL 默认帧：有 ORDER BY 时累计到当前行；无 ORDER BY 时整个分区
            frame = orderBy.isEmpty() ? Frame.wholePartition() : Frame.cumulativeDefault();
        }
        return new WindowSpec(List.copyOf(partitionBy), List.copyOf(orderBy), frame);
    }

    private OrderKey parseOrderKey(Object node) {
        if (node instanceof String s) {
            return new OrderKey(s, Direction.ASC, NullOrder.defaultValue(Direction.ASC));
        }
        Map<String, Object> obj = Json.asObject(node, "orderBy 元素");
        String column = Json.requireString(obj, "column");
        Direction dir = Direction.ASC;
        NullOrder no = null;
        Object d = obj.get("direction");
        if (d instanceof String ds && !ds.isBlank()) {
            dir = Direction.parse(ds);
        }
        Object n = obj.get("nullOrder");
        if (n instanceof String ns && !ns.isBlank()) {
            no = NullOrder.parse(ns);
        }
        return new OrderKey(column, dir, no == null ? NullOrder.defaultValue(dir) : no);
    }

    // ---------- 帧 ----------

    private Frame parseFrame(Object node) {
        if (node == null) {
            return null;
        }
        if (node instanceof String s) {
            return parseFrameText(s);
        }
        Map<String, Object> obj = Json.asObject(node, "window.frame");
        FrameMode mode = FrameMode.ROWS;
        Object m = obj.get("mode");
        if (m instanceof String ms && !ms.isBlank()) {
            mode = FrameMode.parse(ms);
        }
        Object sNode = obj.get("start");
        Object eNode = obj.get("end");
        if (sNode == null || eNode == null) {
            throw new EngineException(ErrorCode.INVALID_FRAME,
                    "frame 对象必须同时提供 start 和 end");
        }
        return new Frame(mode, parseBound(sNode, "start"), parseBound(eNode, "end"));
    }

    private FrameBound parseBound(Object node, String which) {
        if (node instanceof String s) {
            return parseBoundText(s, which);
        }
        Map<String, Object> obj = Json.asObject(node, "frame." + which);
        String typeRaw = Json.requireString(obj, "type");
        FrameBoundType type;
        try {
            type = FrameBoundType.valueOf(typeRaw.toUpperCase().replace(' ', '_'));
        } catch (IllegalArgumentException e) {
            throw new EngineException(ErrorCode.INVALID_FRAME,
                    "非法帧边界类型: " + typeRaw);
        }
        long offset = 0;
        if (obj.containsKey("offset")) {
            offset = Json.requireLong(obj, "offset");
        }
        if ((type == FrameBoundType.PRECEDING || type == FrameBoundType.FOLLOWING)
                && !obj.containsKey("offset")) {
            throw new EngineException(ErrorCode.INVALID_FRAME,
                    "帧边界 " + type + " 必须提供非负整数 offset");
        }
        return new FrameBound(type, offset);
    }

    /** 解析 SQL 风格帧文本，如 "ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING"。 */
    Frame parseFrameText(String text) {
        String t = text.trim().toUpperCase();
        if (!t.startsWith("ROWS")) {
            throw new EngineException(ErrorCode.INVALID_FRAME,
                    "帧文本必须以 ROWS 开头: " + text);
        }
        String rest = t.substring(4).trim();
        if (rest.startsWith("BETWEEN")) {
            String body = rest.substring(7).trim();
            int and = body.indexOf(" AND ");
            if (and < 0) {
                throw new EngineException(ErrorCode.INVALID_FRAME,
                        "BETWEEN 帧缺少 AND: " + text);
            }
            FrameBound start = parseBoundText(body.substring(0, and).trim(), "start");
            FrameBound end = parseBoundText(body.substring(and + 5).trim(), "end");
            return new Frame(FrameMode.ROWS, start, end);
        }
        // 单边界简写：ROWS <bound> 等价 BETWEEN <bound> AND CURRENT ROW
        if (rest.isEmpty()) {
            throw new EngineException(ErrorCode.INVALID_FRAME, "帧文本不完整: " + text);
        }
        return new Frame(FrameMode.ROWS,
                parseBoundText(rest, "start"),
                new FrameBound(FrameBoundType.CURRENT_ROW, 0));
    }

    private FrameBound parseBoundText(String text, String which) {
        String t = text.trim().toUpperCase();
        return switch (t) {
            case "UNBOUNDED PRECEDING" ->
                    new FrameBound(FrameBoundType.UNBOUNDED_PRECEDING, 0);
            case "UNBOUNDED FOLLOWING" ->
                    new FrameBound(FrameBoundType.UNBOUNDED_FOLLOWING, 0);
            case "CURRENT ROW" ->
                    new FrameBound(FrameBoundType.CURRENT_ROW, 0);
            default -> {
                String[] parts = t.split("\\s+");
                if (parts.length != 2) {
                    throw new EngineException(ErrorCode.INVALID_FRAME,
                            "无法解析帧" + which + "边界: " + text);
                }
                long off;
                try {
                    off = Long.parseLong(parts[0]);
                } catch (NumberFormatException e) {
                    throw new EngineException(ErrorCode.INVALID_FRAME,
                            "帧位移必须是整数: " + parts[0]);
                }
                yield switch (parts[1]) {
                    case "PRECEDING" -> new FrameBound(FrameBoundType.PRECEDING, off);
                    case "FOLLOWING" -> new FrameBound(FrameBoundType.FOLLOWING, off);
                    default -> throw new EngineException(ErrorCode.INVALID_FRAME,
                            "无法解析帧" + which + "边界: " + text);
                };
            }
        };
    }

    // ---------- 导出 ----------

    private String parseExport(Object node) {
        if (node == null) {
            return null;
        }
        if (node instanceof String s) {
            return s;
        }
        if (node instanceof Map<?, ?> m) {
            Object dir = m.get("dir");
            if (dir instanceof String ds && !ds.isBlank()) {
                return ds;
            }
            throw new EngineException(ErrorCode.INVALID_REQUEST,
                    "export 对象必须包含非空字符串字段 dir");
        }
        throw new EngineException(ErrorCode.INVALID_REQUEST,
                "export 必须是目录字符串或 {\"dir\": ...} 对象");
    }
}
