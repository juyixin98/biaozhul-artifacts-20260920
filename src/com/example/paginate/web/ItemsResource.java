package com.example.paginate.web;

import com.example.paginate.json.Json;
import com.example.paginate.model.Item;
import com.example.paginate.page.PageResult;
import com.example.paginate.page.PaginationService;
import com.example.paginate.query.PageQuery;
import com.sun.net.httpserver.HttpExchange;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** GET /api/items —— 游标分页查询。 */
public final class ItemsResource {

    private final PaginationService pagination;

    public ItemsResource(PaginationService pagination) {
        this.pagination = pagination;
    }

    public void handle(HttpExchange ex, Map<String, String> params) {
        PageQuery query = PageQuery.fromParams(params);
        String cursor = params.get("cursor");

        PageResult result = (cursor == null || cursor.isEmpty())
                ? pagination.firstPage(query)
                : pagination.nextPage(query, cursor);

        List<Object> itemsJson = new ArrayList<>(result.items().size());
        for (Item item : result.items()) {
            itemsJson.add(item.toJson());
        }

        Map<String, Object> echo = new LinkedHashMap<>();
        echo.put("sort", query.sort());
        echo.put("category", query.category());
        echo.put("q", query.q());
        echo.put("pageSize", query.pageSize());

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("items", itemsJson);
        body.put("nextCursor", result.nextCursor()); // null 表示已到末页
        body.put("hasMore", result.hasMore());
        body.put("snapshotId", result.snapshotId());
        body.put("snapshotVersion", result.snapshotVersion());
        body.put("pageSize", result.pageSize());
        body.put("count", result.items().size());
        body.put("query", echo);

        HttpSupport.sendJson(ex, 200, body);
    }

    static String json(Object body) {
        return Json.stringify(body);
    }
}
