package com.example.paginate.web;

import com.example.paginate.cursor.CursorService;
import com.example.paginate.page.PaginationService;
import com.example.paginate.snapshot.SnapshotManager;
import com.example.paginate.store.ItemStore;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * 应用装配与 JDK HttpServer 路由。
 *
 * 路由：
 *   GET    /api/items              分页查询（sort/category/q/pageSize/cursor）
 *   POST   /api/admin/items        插入
 *   GET    /api/admin/items/{id}   单条查询
 *   PATCH  /api/admin/items/{id}   修改（可改排序键 name/score）
 *   DELETE /api/admin/items/{id}   删除
 *   GET    /api/admin/stats        数据规模
 *   GET    /healthz                健康检查
 */
public final class HttpServerApp {

    private static final Logger LOG = Logger.getLogger(HttpServerApp.class.getName());

    private final int port;
    private final Duration snapshotTtl;
    private final String cursorSecret;

    private HttpServer server;
    private ExecutorService pool;

    private ItemStore store;
    private SnapshotManager snapshotManager;
    private CursorService cursorService;
    private PaginationService pagination;

    public HttpServerApp(int port, Duration snapshotTtl, String cursorSecret) {
        this.port = port;
        this.snapshotTtl = snapshotTtl;
        this.cursorSecret = cursorSecret;
    }

    public SnapshotManager snapshotManager() {
        return snapshotManager;
    }

    public CursorService cursorService() {
        return cursorService;
    }

    public ItemStore store() {
        return store;
    }

    public int start() throws IOException {
        store = new ItemStore();
        snapshotManager = new SnapshotManager(snapshotTtl);
        cursorService = new CursorService(cursorSecret);
        pagination = new PaginationService(store, snapshotManager, cursorService);

        ItemsResource itemsResource = new ItemsResource(pagination);
        AdminResource adminResource = new AdminResource(store);

        server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/", exchange -> {
            try {
                route(exchange, itemsResource, adminResource);
            } catch (ApiException e) {
                sendError(exchange, e);
            } catch (RuntimeException e) {
                LOG.log(Level.WARNING, "未处理异常", e);
                sendError(exchange, new ApiException(500, "internal_error", "服务器内部错误"));
            } finally {
                exchange.close();
            }
        });
        pool = Executors.newFixedThreadPool(8);
        server.setExecutor(pool);
        server.start();
        return server.getAddress().getPort();
    }

    public void stop() {
        if (server != null) {
            server.stop(0);
        }
        if (pool != null) {
            pool.shutdownNow();
        }
    }

    private void route(HttpExchange ex, ItemsResource itemsResource, AdminResource adminResource)
            throws IOException {
        String method = ex.getRequestMethod().toUpperCase();
        String path = ex.getRequestURI().getPath();
        Map<String, String> params = QueryParams.parse(ex.getRequestURI().getRawQuery());

        switch (path) {
            case "/healthz" -> {
                requireGet(method);
                Map<String, Object> body = new LinkedHashMap<>();
                body.put("status", "ok");
                body.put("snapshotTtlSeconds", snapshotTtl.getSeconds());
                body.put("retainedSnapshots", snapshotManager.retainedCount());
                body.put("items", store.size());
                HttpSupport.sendJson(ex, 200, body);
            }
            case "/api/items" -> {
                requireGet(method);
                itemsResource.handle(ex, params);
            }
            default -> {
                if (path.startsWith("/api/admin/")) {
                    adminResource.route(ex, method, path);
                } else {
                    throw ApiException.notFound("not_found", "无此接口: " + method + " " + path);
                }
            }
        }
    }

    private static void requireGet(String method) {
        if (!"GET".equals(method)) {
            throw new ApiException(405, "method_not_allowed", "该接口只支持 GET，实际为 " + method);
        }
    }

    private static void sendError(HttpExchange ex, ApiException e) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", e.code());
        body.put("message", e.getMessage());
        try {
            HttpSupport.sendJson(ex, e.httpStatus(), body);
        } catch (RuntimeException ignored) {
            // 连错误响应都写不出去（客户端断开），放弃
        }
    }
}
