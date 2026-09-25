package com.bm25pager.search;

import com.bm25pager.json.Json;

import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 翻页游标。游标内容是自描述的，服务端不保存任何会话状态：
 *
 *   v         绑定的索引快照版本（第一页之后所有翻页都在同一快照上进行）
 *   t         规范化后的查询词项（小写 token 数组，去重在打分阶段完成）
 *   ps        该次查询固定的页大小
 *   page      当前返回页的页码（0 基）
 *   lastScore 上一页最后一条命中的 BM25 分数
 *   lastId    上一页最后一条命中的文档 ID
 *
 * 存储形式：上述 JSON 的 URL-safe Base64（无填充）。
 * lastScore/lastId 与“score 降序、docId 升序”的排序规则一起唯一定位续读位置；
 * 浮点分数按 double 原样存取，保证切片边界精确，不发生漏读或重读。
 */
public final class Cursor {

    public final long version;
    public final List<String> terms;
    public final int pageSize;
    public final int page;
    public final double lastScore;
    public final String lastId;

    public Cursor(long version,
                  List<String> terms,
                  int pageSize,
                  int page,
                  double lastScore,
                  String lastId) {
        this.version = version;
        this.terms = List.copyOf(terms);
        this.pageSize = pageSize;
        this.page = page;
        this.lastScore = lastScore;
        this.lastId = lastId;
    }

    /** 第一页游标的工厂（尚未指向任何记录）。 */
    public static Cursor firstPage(long version, List<String> terms, int pageSize) {
        return new Cursor(version, terms, pageSize, 0, Double.NaN, "");
    }

    public String encode() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("v", version);
        m.put("t", terms);
        m.put("ps", pageSize);
        m.put("page", page);
        m.put("lastScore", lastScore);
        m.put("lastId", lastId == null ? "" : lastId);
        byte[] json = Json.write(m).getBytes(StandardCharsets.UTF_8);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(json);
    }

    /**
     * 解码外部传入的游标。格式损坏、字段缺失/类型错误统一抛 InvalidCursorException，
     * HTTP 层映射为 400 INVALID_CURSOR（与 410 SNAPSHOT_EXPIRED 区分）。
     */
    public static Cursor decode(String encoded) {
        if (encoded == null || encoded.isEmpty()) {
            throw new InvalidCursorException("cursor is empty");
        }
        final byte[] raw;
        try {
            raw = Base64.getUrlDecoder().decode(encoded);
        } catch (IllegalArgumentException iae) {
            throw new InvalidCursorException("cursor is not valid base64");
        }
        final Object parsed;
        try {
            parsed = Json.parse(new String(raw, StandardCharsets.UTF_8));
        } catch (Json.JsonException je) {
            throw new InvalidCursorException("cursor payload is not valid JSON");
        }
        if (!(parsed instanceof Map<?, ?> m)) {
            throw new InvalidCursorException("cursor payload is not a JSON object");
        }
        try {
            long version = ((Number) require(m, "v")).longValue();
            int pageSize = ((Number) require(m, "ps")).intValue();
            int page = ((Number) require(m, "page")).intValue();
            double lastScore = ((Number) require(m, "lastScore")).doubleValue();
            Object lastIdRaw = require(m, "lastId");
            String lastId = lastIdRaw == null ? "" : (String) lastIdRaw;

            Object termsRaw = require(m, "t");
            if (!(termsRaw instanceof List<?> termsList)) {
                throw new InvalidCursorException("cursor field 't' must be an array");
            }
            List<String> terms;
            try {
                @SuppressWarnings("unchecked")
                List<String> casted = (List<String>) termsList;
                terms = List.copyOf(casted);
            } catch (ClassCastException cce) {
                throw new InvalidCursorException("cursor field 't' must contain only strings");
            }
            if (version < 1 || pageSize < 1 || page < 0) {
                throw new InvalidCursorException("cursor contains non-positive numeric fields");
            }
            return new Cursor(version, terms, pageSize, page, lastScore, lastId);
        } catch (InvalidCursorException ice) {
            throw ice;
        } catch (RuntimeException re) {
            throw new InvalidCursorException("cursor has missing or mistyped fields: " + re.getMessage());
        }
    }

    private static Object require(Map<?, ?> m, String key) {
        if (!m.containsKey(key)) {
            throw new InvalidCursorException("cursor missing field: " + key);
        }
        return m.get(key);
    }
}
