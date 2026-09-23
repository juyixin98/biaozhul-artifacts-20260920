package tvl;

import java.util.LinkedHashMap;
import java.util.Map;

import tvl.core.DataType;
import tvl.core.TriBool;
import tvl.core.Value;
import tvl.engine.RequestHandler;
import tvl.eval.Evaluator;
import tvl.parser.Expr;
import tvl.parser.Parser;
import tvl.parser.TypeChecker;

/** 测试辅助：编译表达式、对单行求值、发送 JSON 请求。 */
public final class Harness {

    private Harness() {}

    public static Map<String, DataType> schema(Object... nameType) {
        Map<String, DataType> m = new LinkedHashMap<>();
        for (int i = 0; i < nameType.length; i += 2) {
            m.put((String) nameType[i], (DataType) nameType[i + 1]);
        }
        return m;
    }

    public static Expr compile(String source, Map<String, DataType> schema) {
        Expr ast = Parser.parse(source);
        new TypeChecker(schema).check(ast);
        return ast;
    }

    public static Expr compile(String source) {
        return compile(source, new LinkedHashMap<>());
    }

    /** Object[] 转 Value[]：Long/Integer→INTEGER，Boolean→BOOLEAN，String→STRING，null→NULL。 */
    public static Value[] row(Object... cells) {
        Value[] vs = new Value[cells.length];
        for (int i = 0; i < cells.length; i++) {
            Object o = cells[i];
            if (o == null) {
                vs[i] = Value.NULL;
            } else if (o instanceof Long || o instanceof Integer) {
                vs[i] = Value.ofInteger(((Number) o).longValue());
            } else if (o instanceof Boolean) {
                vs[i] = Value.ofBoolean((Boolean) o);
            } else if (o instanceof String) {
                vs[i] = Value.ofString((String) o);
            } else if (o instanceof Value) {
                vs[i] = (Value) o;
            } else {
                throw new IllegalArgumentException("不支持的测试值：" + o);
            }
        }
        return vs;
    }

    public static Value[] rowV(Value... cells) {
        return cells;
    }

    /** 与 {@link #row(Object...)} 相同，语义化别名。 */
    public static Value[] cells(Object... cells) {
        return row(cells);
    }

    public static TriBool logic(String source, Map<String, DataType> schema,
                                String[] colNames, Value[] row) {
        Expr ast = compile(source, schema);
        return new Evaluator(colNames).evalLogic(ast, row);
    }

    /** 单列便捷求值。 */
    public static TriBool logic1(String source, String col, DataType type, Value v) {
        return logic(source, schema(col, type), new String[]{col}, rowV(v));
    }

    public static Value value(String source, Map<String, DataType> schema,
                              String[] colNames, Value[] row) {
        Expr ast = compile(source, schema);
        return new Evaluator(colNames).eval(ast, row);
    }

    public static Value value(String source) {
        Expr ast = compile(source);
        return new Evaluator(new String[0]).eval(ast, new Value[0]);
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> request(String json) {
        RequestHandler.Response resp = new RequestHandler().handle(json);
        return (Map<String, Object>) (Map<?, ?>) resp.body;
    }
}
