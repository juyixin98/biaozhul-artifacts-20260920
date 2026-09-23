package vecq.test;

import vecq.Catalog;
import vecq.QueryEngine;
import vecq.QueryResult;

import java.util.List;
import java.util.Map;

/** 测试辅助：对 orders 表执行请求并取出 selectedRows。 */
public final class Q {

    private Q() {}

    public static QueryResult run(String requestJson) {
        return new QueryEngine(new Catalog()).execute(JsonLike.parse(requestJson));
    }

    public static List<Object> rows(QueryResult r) {
        @SuppressWarnings("unchecked")
        List<Object> rows = (List<Object>) r.toResponseJson().get("selectedRows");
        return rows;
    }

    public static List<Object> rows(String requestJson) {
        return rows(run(requestJson));
    }

    public static Map<String, Object> resp(String requestJson) {
        return run(requestJson).toResponseJson();
    }

    /** 避免在测试辅助类里到处写 vecq.Json 的小别名。 */
    static final class JsonLike {
        static Object parse(String s) { return vecq.Json.parse(s); }
    }
}
