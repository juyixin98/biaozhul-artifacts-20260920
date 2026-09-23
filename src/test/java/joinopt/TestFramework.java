package joinopt;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 零依赖断言测试框架：各测试方法以 void 形式抛出 AssertionError，
 * 由 main 逐个运行并统计通过/失败。
 */
public final class TestFramework {

    private TestFramework() {}

    public static void check(boolean cond, String msg) {
        if (!cond) throw new AssertionError(msg);
    }

    public static void eq(Object actual, Object expected, String msg) {
        if (actual == null ? expected != null : !actual.equals(expected)) {
            throw new AssertionError(msg + " — 期望: " + expected + ", 实际: " + actual);
        }
    }

    public static void approx(double actual, double expected, double tol, String msg) {
        if (Math.abs(actual - expected) > tol) {
            throw new AssertionError(msg + " — 期望≈ " + expected + ", 实际: " + actual);
        }
    }

    /** 从 JSON 字符串构造表（默认无 ndv，按数据计算）。 */
    public static List<Table> tables(String json, List<String> warnings) {
        Map<String, Object> req = Json.asObj(Json.parse(json));
        List<Table> out = new ArrayList<>();
        for (Object tj : Json.arr(req, "tables")) out.add(Table.fromJson(Json.asObj(tj), warnings));
        return out;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> obj(String json) {
        return (Map<String, Object>) Json.parse(json);
    }

    public static List<JoinPred> preds(String json, List<Table> tables, List<String> warnings) {
        Map<String, Object> req = Json.asObj(Json.parse(json));
        Map<String, Integer> idx = new LinkedHashMap<>();
        for (int i = 0; i < tables.size(); i++) idx.put(tables.get(i).name, i);
        List<JoinPred> out = new ArrayList<>();
        for (Object pj : Json.arr(req, "joins")) out.add(JoinPred.fromJson(pj, idx, "p"));
        return out;
    }
}
