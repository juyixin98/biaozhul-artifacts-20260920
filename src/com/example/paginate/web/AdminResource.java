package com.example.paginate.web;

import com.example.paginate.json.Json;
import com.example.paginate.model.Item;
import com.example.paginate.store.ItemStore;
import com.sun.net.httpserver.HttpExchange;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;

/**
 * 管理端接口（用于在分页过程中制造插入/删除/改排序键）：
 *   POST   /api/admin/items        新建（body: {id?,name,category?,score?}）
 *   GET    /api/admin/items/{id}   查看单条
 *   PATCH  /api/admin/items/{id}   修改（body: {name?,category?,score?}，仅提供的字段被更新）
 *   DELETE /api/admin/items/{id}   删除
 *   GET    /api/admin/stats        当前真实数据规模
 */
public final class AdminResource {

    private final ItemStore store;

    public AdminResource(ItemStore store) {
        this.store = store;
    }

    public void route(HttpExchange ex, String method, String path) {
        switch (method) {
            case "POST" -> {
                if (path.equals("/api/admin/items")) {
                    create(ex);
                } else {
                    throw ApiException.notFound("not_found", "无此接口: " + method + " " + path);
                }
            }
            case "GET" -> {
                if (path.equals("/api/admin/stats")) {
                    stats(ex);
                } else if (path.startsWith("/api/admin/items/")) {
                    getOne(ex, idFromPath(path));
                } else {
                    throw ApiException.notFound("not_found", "无此接口: " + method + " " + path);
                }
            }
            case "PATCH" -> {
                if (path.startsWith("/api/admin/items/")) {
                    update(ex, idFromPath(path));
                } else {
                    throw ApiException.notFound("not_found", "无此接口: " + method + " " + path);
                }
            }
            case "DELETE" -> {
                if (path.startsWith("/api/admin/items/")) {
                    delete(ex, idFromPath(path));
                } else {
                    throw ApiException.notFound("not_found", "无此接口: " + method + " " + path);
                }
            }
            default -> throw new ApiException(405, "method_not_allowed",
                    "不支持的 HTTP 方法: " + method);
        }
    }

    private void create(HttpExchange ex) {
        Map<String, Object> body = HttpSupport.readJsonObject(ex);
        String name = requireString(body, "name");
        String category = Json.getString(body, "category");
        if (category == null) {
            category = "default";
        }
        Long score = Json.getLong(body, "score");
        Long id = Json.getLong(body, "id");

        Item item = store.create(id, name, category, score == null ? 0L : score);
        if (item == null) {
            throw ApiException.conflict("id_conflict", "id 已存在: " + id);
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("item", item.toJson());
        HttpSupport.sendJson(ex, 201, resp);
    }

    private void getOne(HttpExchange ex, long id) {
        Optional<Item> item = store.find(id);
        if (item.isEmpty()) {
            throw ApiException.notFound("item_not_found", "记录不存在: " + id);
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("item", item.get().toJson());
        HttpSupport.sendJson(ex, 200, resp);
    }

    private void update(HttpExchange ex, long id) {
        Map<String, Object> body = HttpSupport.readJsonObject(ex);
        String name = body.containsKey("name") ? requireString(body, "name") : null;
        String category = body.containsKey("category") ? Json.getString(body, "category") : null;
        Long score = body.containsKey("score") ? Json.getLong(body, "score") : null;

        Optional<Item> item = store.update(id, name, category, score);
        if (item.isEmpty()) {
            throw ApiException.notFound("item_not_found", "记录不存在: " + id);
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("item", item.get().toJson());
        HttpSupport.sendJson(ex, 200, resp);
    }

    private void delete(HttpExchange ex, long id) {
        boolean deleted = store.delete(id);
        if (!deleted) {
            throw ApiException.notFound("item_not_found", "记录不存在: " + id);
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("deleted", true);
        resp.put("id", id);
        HttpSupport.sendJson(ex, 200, resp);
    }

    private void stats(HttpExchange ex) {
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("total", store.size());
        HttpSupport.sendJson(ex, 200, resp);
    }

    private static long idFromPath(String path) {
        String raw = path.substring("/api/admin/items/".length());
        if (raw.isEmpty() || raw.contains("/")) {
            throw ApiException.badRequest("invalid_id", "路径中的 id 非法: " + raw);
        }
        try {
            return Long.parseLong(raw);
        } catch (NumberFormatException e) {
            throw ApiException.badRequest("invalid_id", "id 必须是整数: " + raw);
        }
    }

    private static String requireString(Map<String, Object> body, String field) {
        Object v = body.get(field);
        if (!(v instanceof String s) || s.isEmpty()) {
            throw ApiException.badRequest("invalid_field", "字段 " + field + " 必须是非空字符串");
        }
        return s;
    }
}
