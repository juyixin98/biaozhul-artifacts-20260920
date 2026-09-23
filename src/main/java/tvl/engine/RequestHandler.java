package tvl.engine;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import tvl.core.CompileException;
import tvl.core.DataType;
import tvl.core.EvalException;
import tvl.core.Pos;
import tvl.core.Value;
import tvl.eval.Evaluator;
import tvl.core.TriBool;
import tvl.json.BadRequestException;
import tvl.json.Json;
import tvl.parser.Expr;
import tvl.parser.Parser;
import tvl.parser.TypeChecker;

/**
 * JSON 请求入口：解析请求 → 构造内存表 → 解析+类型检查表达式 →
 * 生成执行计划 → 执行查询 → 装配响应。
 *
 * 请求结构（字段大小写敏感）：
 * <pre>
 * {
 *   "expression": "a > 1 AND b IS NOT NULL",
 *   "table": {
 *     "name": "t",
 *     "columns": [{"name":"a","type":"INTEGER"}, ...],
 *     "rows": [[1, null], ...]
 *   },
 *   "includeTable": true
 * }
 * </pre>
 * 无 table 字段时按纯表达式处理：编译通过后对“空行”求值一次，
 * 返回表达式自身的标量结果与（对布尔表达式的）三值判定。
 */
public final class RequestHandler {

    public static final class Response {
        public final boolean ok;
        public final Map<String, Object> body;

        Response(boolean ok, Map<String, Object> body) {
            this.ok = ok;
            this.body = body;
        }

        public String toJson() {
            return Json.pretty(body);
        }
    }

    public Response handle(String requestJson) {
        Map<String, Object> req;
        try {
            req = Json.parseObject(requestJson);
        } catch (RuntimeException ex) {
            return error("INVALID_JSON", "请求不是合法 JSON：" + ex.getMessage(),
                    null, null);
        }

        try {
            return doHandle(req);
        } catch (BadRequestException ex) {
            return error("BAD_REQUEST", ex.getMessage(), null, null);
        } catch (CompileException ex) {
            return error(ex.code(), ex.getMessage(), ex.pos(), ex.length());
        } catch (EvalException ex) {
            return error(ex.code(), ex.getMessage(), null, null);
        }
    }

    private Response doHandle(Map<String, Object> req) {
        Object exprField = req.get("expression");
        if (exprField == null) {
            throw new BadRequestException("缺少字段 'expression'（过滤表达式）");
        }
        if (!(exprField instanceof String)) {
            throw new BadRequestException("字段 'expression' 必须是字符串");
        }
        String source = (String) exprField;

        boolean hasTable = req.containsKey("table") && req.get("table") != null;
        Table table = hasTable
                ? buildTable(Json.requireObject(req.get("table"), "字段 'table'"))
                : new Table("(inline)", new ArrayList<>());
        boolean includeTable = Boolean.TRUE.equals(req.get("includeTable"));

        // 1) 语法解析
        Expr ast = Parser.parse(source);

        // 2) 类型检查
        TypeChecker checker = new TypeChecker(table.schemaMap());
        checker.check(ast);

        // 3) 执行计划
        QueryEngine engine = new QueryEngine(table, ast);
        Map<String, Object> plan = engine.explain();

        // 4) 执行
        Map<String, Object> result;
        if (hasTable) {
            result = engine.execute().toJson(table);
        } else {
            result = evalStandalone(ast);
        }

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("ok", true);
        body.put("expression", source);
        body.put("ast", ast.toJson());
        body.put("plan", plan);
        body.put("result", result);
        if (includeTable && hasTable) {
            body.put("table", table.toJson());
        }
        return new Response(true, body);
    }

    /** 无表纯表达式：编译通过后对空行求值一次，返回标量值/三值。 */
    private Map<String, Object> evalStandalone(Expr ast) {
        Map<String, Object> m = new LinkedHashMap<>();
        Evaluator evaluator = new Evaluator(new String[0]);
        Value v = evaluator.eval(ast, new Value[0]);

        m.put("mode", "standaloneExpression");
        m.put("value", v.describe());

        if (v.type() == DataType.BOOLEAN || v.isNull()) {
            TriBool tri = evaluator.evalLogic(ast, new Value[0]);
            m.put("triValue", tri.name());
        }
        return m;
    }

    // ---------- 表装载 ----------

    private Table buildTable(Map<String, Object> tableNode) {
        String tableName = Json.optionalString(tableNode, "name", "t");

        Object colsNode = tableNode.get("columns");
        if (colsNode == null) {
            throw new BadRequestException("table 中缺少 'columns'（列定义数组）");
        }
        List<Object> cols = Json.requireArray(colsNode, "table.columns");
        List<Table.Column> columns = new ArrayList<>();
        List<String> names = new ArrayList<>();
        for (int i = 0; i < cols.size(); i++) {
            Map<String, Object> c = Json.requireObject(cols.get(i),
                    "table.columns[" + i + "]");
            String name = Json.requireString(c, "name");
            String typeStr = Json.requireString(c, "type").toUpperCase();
            DataType type;
            try {
                type = DataType.valueOf(typeStr);
            } catch (IllegalArgumentException ex) {
                throw new BadRequestException("不支持的列类型 '" + typeStr
                        + "'（支持 INTEGER / BOOLEAN / STRING）");
            }
            if (type == DataType.NULL) {
                throw new BadRequestException("列类型不能为 NULL（NULL 只是取值）");
            }
            if (names.contains(name)) {
                throw new BadRequestException("列名重复：" + name);
            }
            names.add(name);
            columns.add(new Table.Column(name, type));
        }

        Object rowsNode = tableNode.get("rows");
        List<Object> rawRows = rowsNode == null
                ? new ArrayList<>()
                : Json.requireArray(rowsNode, "table.rows");

        Table table = new Table(tableName, columns);
        for (int i = 0; i < rawRows.size(); i++) {
            List<Object> rawRow = Json.requireArray(rawRows.get(i),
                    "table.rows[" + i + "]");
            if (rawRow.size() != columns.size()) {
                throw new BadRequestException("table.rows[" + i
                        + "] 列数不匹配：期望 " + columns.size()
                        + "，实际 " + rawRow.size());
            }
            Value[] row = new Value[columns.size()];
            for (int j = 0; j < columns.size(); j++) {
                row[j] = convertCell(rawRow.get(j), columns.get(j), i, j);
            }
            table.addRow(row);
        }
        return table;
    }

    private Value convertCell(Object v, Table.Column col, int rowIdx, int colIdx) {
        String where = "table.rows[" + rowIdx + "][" + colIdx + "]（列 "
                + col.name + "）";
        if (v == null) {
            return Value.NULL;
        }
        switch (col.type) {
            case INTEGER:
                if (v instanceof Long) {
                    return Value.ofInteger((Long) v);
                }
                if (v instanceof Integer) {
                    return Value.ofInteger(((Integer) v).longValue());
                }
                if (v instanceof Double || v instanceof Float) {
                    throw new BadRequestException(where
                            + " 必须是 JSON 整数，不接受小数 " + v);
                }
                throw new BadRequestException(where + " 必须是整数，实际为 "
                        + v.getClass().getSimpleName());
            case BOOLEAN:
                if (v instanceof Boolean) {
                    return Value.ofBoolean((Boolean) v);
                }
                throw new BadRequestException(where + " 必须是布尔值 true/false");
            case STRING:
                if (v instanceof String) {
                    return Value.ofString((String) v);
                }
                throw new BadRequestException(where + " 必须是字符串");
            default:
                throw new BadRequestException(where + " 不支持的列类型");
        }
    }

    // ---------- 错误响应 ----------

    private Response error(String code, String message, Pos pos, Integer length) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("ok", false);
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("code", code);
        err.put("message", message);
        if (pos != null) {
            err.put("position", pos.toJson());
            if (length != null) {
                err.put("length", length);
            }
        }
        body.put("error", err);
        return new Response(false, body);
    }
}
