package com.bm25stable;

import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.Map;

/**
 * 分页游标。绑定索引快照版本与查询条件，采用键集（keyset）语义：
 * 记录上一页最后一条命中的 (score, docId)，下一页从严格小于该键的位置继续。
 *
 * <p>线上形式为 base64url(JSON)，例如：
 * <pre>{"v":3,"q":"hello world","ps":10,"s":1.234,"id":"doc-7","off":10}</pre>
 *
 * <p>字段：v=快照版本，q=规范化查询（去重后按首次出现顺序拼接），ps=每页大小，
 * s=上页末条得分，id=上页末条文档 id，off=已返回条数（仅用于展示页码进度）。
 */
public record SearchCursor(int version, String query, int pageSize, double lastScore, String lastDocId, int offset) {

    public String encode() {
        String json = Json.stringify(Map.of(
                "v", version,
                "q", query,
                "ps", pageSize,
                "s", lastScore,
                "id", lastDocId,
                "off", offset));
        return Base64.getUrlEncoder().withoutPadding()
                .encodeToString(json.getBytes(StandardCharsets.UTF_8));
    }

    public static SearchCursor decode(String encoded) {
        final String json;
        try {
            String padded = encoded + "=".repeat((4 - encoded.length() % 4) % 4);
            json = new String(Base64.getUrlDecoder().decode(padded), StandardCharsets.UTF_8);
        } catch (IllegalArgumentException e) {
            throw new SearchException.InvalidCursor("cursor is not valid base64url: " + e.getMessage());
        }
        final Object parsed;
        try {
            parsed = Json.parse(json);
        } catch (IllegalArgumentException e) {
            throw new SearchException.InvalidCursor("cursor payload is not valid JSON: " + e.getMessage());
        }
        if (!(parsed instanceof Map<?, ?> map)) {
            throw new SearchException.InvalidCursor("cursor payload must be a JSON object");
        }
        try {
            int version = ((Number) require(map, "v")).intValue();
            String query = (String) require(map, "q");
            int pageSize = ((Number) require(map, "ps")).intValue();
            double lastScore = ((Number) require(map, "s")).doubleValue();
            String lastDocId = (String) require(map, "id");
            int offset = ((Number) require(map, "off")).intValue();
            return new SearchCursor(version, query, pageSize, lastScore, lastDocId, offset);
        } catch (ClassCastException | NullPointerException e) {
            throw new SearchException.InvalidCursor("cursor payload has wrong field types");
        }
    }

    private static Object require(Map<?, ?> map, String key) {
        Object value = map.get(key);
        if (value == null) {
            throw new SearchException.InvalidCursor("cursor payload missing field: " + key);
        }
        return value;
    }
}
