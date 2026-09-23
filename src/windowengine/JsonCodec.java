package windowengine;

import windowengine.plan.Frame;
import windowengine.plan.FrameBound;
import windowengine.plan.FunctionCall;
import windowengine.plan.OrderKey;
import windowengine.plan.QueryPlan;
import windowengine.plan.WindowFunction;
import windowengine.plan.WindowSpec;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 把内存对象序列化为可导出 / 可回读的 JSON 结构：
 * - 关系序列化为 {"columns":[{name,type}],"rows":[[...]]}，与请求中的 data 同构，
 *   因此导出的数据文件可以直接作为另一个请求的 data.file 回读；
 * - 执行计划导出为规范化 JSON（默认值被显式展开，帧同时给出文本与结构形式）。
 */
public final class JsonCodec {

    private JsonCodec() {
    }

    // ---------- 关系 ----------

    public static Map<String, Object> relationToJson(Relation relation) {
        List<Object> columns = new ArrayList<>();
        for (int i = 0; i < relation.schema().size(); i++) {
            Map<String, Object> col = new LinkedHashMap<>();
            col.put("name", relation.schema().name(i));
            col.put("type", relation.schema().type(i).name());
            columns.add(col);
        }

        List<Object> rows = new ArrayList<>(relation.rowCount());
        for (Row row : relation.rows()) {
            List<Object> cells = new ArrayList<>(relation.schema().size());
            for (int c = 0; c < relation.schema().size(); c++) {
                cells.add(valueToJson(row.get(c)));
            }
            rows.add(cells);
        }

        Map<String, Object> root = new LinkedHashMap<>();
        root.put("columns", columns);
        root.put("rows", rows);
        return root;
    }

    private static Object valueToJson(Value v) {
        return switch (v.type()) {
            case NULL -> null;
            case LONG -> v.asLong();
            case STRING -> v.asString();
        };
    }

    // ---------- 执行计划 ----------

    public static Map<String, Object> planToJson(QueryPlan plan) {
        Map<String, Object> window = new LinkedHashMap<>();
        window.put("partitionBy", plan.window().partitionBy());

        List<Object> order = new ArrayList<>();
        for (OrderKey key : plan.window().orderBy()) {
            Map<String, Object> k = new LinkedHashMap<>();
            k.put("column", key.column());
            k.put("direction", key.direction().name());
            k.put("nullOrder", key.nullOrder().name());
            k.put("nullOrderIsDefault",
                    key.nullOrder() == windowengine.plan.NullOrder.defaultValue(key.direction()));
            order.add(k);
        }
        window.put("orderBy", order);
        window.put("frame", frameToJson(plan.window().frame()));

        List<Object> functions = new ArrayList<>();
        for (FunctionCall fn : plan.functions()) {
            Map<String, Object> f = new LinkedHashMap<>();
            f.put("function", fn.function().name());
            f.put("alias", fn.alias());
            if (fn.function() == WindowFunction.SUM) {
                f.put("column", fn.argumentColumn());
            }
            functions.add(f);
        }

        Map<String, Object> root = new LinkedHashMap<>();
        root.put("input", plan.input());
        root.put("window", window);
        root.put("functions", functions);
        return root;
    }

    private static Map<String, Object> frameToJson(Frame frame) {
        Map<String, Object> f = new LinkedHashMap<>();
        f.put("mode", frame.mode().name());
        f.put("start", boundToJson(frame.start()));
        f.put("end", boundToJson(frame.end()));
        f.put("sqlText", frame.describe());
        return f;
    }

    private static Map<String, Object> boundToJson(FrameBound bound) {
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("type", bound.type().name());
        if (bound.type() == windowengine.plan.FrameBoundType.PRECEDING
                || bound.type() == windowengine.plan.FrameBoundType.FOLLOWING) {
            b.put("offset", bound.offset());
        }
        return b;
    }
}
