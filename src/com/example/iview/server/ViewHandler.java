package com.example.iview.server;

import com.example.iview.json.Json;
import com.example.iview.view.ApplyResult;
import com.example.iview.view.CategoryAggregate;
import com.example.iview.view.MaterializedView;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;

/** Routes and adapts HTTP requests onto the current {@link MaterializedView}. */
final class ViewHandler implements HttpHandler {

    private final AtomicReference<MaterializedView> state;

    ViewHandler(AtomicReference<MaterializedView> state) {
        this.state = state;
    }

    private MaterializedView view() {
        return state.get();
    }

    @Override
    public void handle(HttpExchange exchange) throws IOException {
        try {
            route(exchange);
        } catch (IllegalArgumentException e) {
            send(exchange, 400, Map.of("error", e.getMessage() == null ? "bad request" : e.getMessage()));
        } catch (Exception e) {
            send(exchange, 500, Map.of("error", String.valueOf(e)));
        } finally {
            exchange.close();
        }
    }

    private void route(HttpExchange exchange) throws IOException {
        String method = exchange.getRequestMethod();
        String path = exchange.getRequestURI().getPath();

        switch (method + " " + path) {
            case "POST /events" -> handleSingleEvent(exchange);
            case "POST /events/batch" -> handleBatch(exchange);
            case "GET /view" -> send(exchange, 200, view().describe());
            case "GET /verify" -> handleVerify(exchange);
            case "GET /tables" -> handleTables(exchange);
            case "POST /reset" -> handleReset(exchange);
            default -> send(exchange, 404, Map.of("error", "not found: " + method + " " + path));
        }
    }

    private void handleSingleEvent(HttpExchange exchange) throws IOException {
        Map<String, Object> body = Json.parseObject(readBody(exchange));
        ApplyResult result = EventParser.apply(view(), body);
        Map<String, Object> response = new LinkedHashMap<>();
        response.put("result", toJson(result));
        response.put("view", view().describe());
        response.put("verified", view().matchesFullRecompute());
        send(exchange, 200, response);
    }

    @SuppressWarnings("unchecked")
    private void handleBatch(HttpExchange exchange) throws IOException {
        Map<String, Object> body = Json.parseObject(readBody(exchange));
        Object rawEvents = body.get("events");
        if (!(rawEvents instanceof List<?> list)) {
            throw new IllegalArgumentException("'events' array is required");
        }
        List<Object> results = new ArrayList<>();
        int index = 0;
        for (Object item : list) {
            if (!(item instanceof Map<?, ?>)) {
                throw new IllegalArgumentException("events[" + index + "] must be an object");
            }
            results.add(toJson(EventParser.apply(view(), (Map<String, Object>) item)));
            index++;
        }
        Map<String, Object> response = new LinkedHashMap<>();
        response.put("results", results);
        response.put("view", view().describe());
        response.put("verified", view().matchesFullRecompute());
        send(exchange, 200, response);
    }

    private void handleVerify(HttpExchange exchange) throws IOException {
        Map<String, CategoryAggregate> incremental = view().aggregates();
        Map<String, CategoryAggregate> full = view().fullRecompute();
        boolean ok = incremental.equals(full);

        List<Object> rows = new ArrayList<>();
        var keys = new java.util.TreeSet<String>();
        keys.addAll(incremental.keySet());
        keys.addAll(full.keySet());
        for (String key : keys) {
            CategoryAggregate a = incremental.get(key);
            CategoryAggregate b = full.get(key);
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("category", key);
            row.put("incremental", a == null ? null : aggJson(a));
            row.put("fullRecompute", b == null ? null : aggJson(b));
            row.put("equal", java.util.Objects.equals(a, b));
            rows.add(row);
        }
        Map<String, Object> response = new LinkedHashMap<>();
        response.put("matches", ok);
        response.put("comparison", rows);
        send(exchange, 200, response);
    }

    private void handleTables(HttpExchange exchange) throws IOException {
        Map<String, Object> response = new LinkedHashMap<>();
        List<Object> products = new ArrayList<>();
        view().products().forEach(p -> {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("productId", p.productId());
            m.put("category", p.category());
            products.add(m);
        });
        List<Object> lines = new ArrayList<>();
        view().orderLines().forEach(l -> {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("orderLineId", l.orderLineId());
            m.put("productId", l.productId());
            m.put("qty", l.qty());
            m.put("amount", l.amount().toPlainString());
            lines.add(m);
        });
        response.put("products", products);
        response.put("orderLines", lines);
        send(exchange, 200, response);
    }

    private void handleReset(HttpExchange exchange) throws IOException {
        state.set(new MaterializedView());
        send(exchange, 200, Map.of("reset", true));
    }

    private static Map<String, Object> aggJson(CategoryAggregate a) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("qty", a.qty());
        m.put("amount", a.amount().toPlainString());
        return m;
    }

    static Map<String, Object> toJson(ApplyResult r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("eventId", r.eventId());
        m.put("duplicate", r.duplicate());
        m.put("side", r.side());
        m.put("op", r.op());
        m.put("changed", r.changed());
        m.put("notFound", r.notFound());
        if (r.categoryFrom() != null) {
            m.put("categoryFrom", r.categoryFrom());
        }
        if (r.categoryTo() != null) {
            m.put("categoryTo", r.categoryTo());
        }
        m.put("migratedOrderLines", r.migratedOrderLines());
        m.put("unmatchedOrderLines", r.unmatchedOrderLines());
        return m;
    }

    private static String readBody(HttpExchange exchange) throws IOException {
        byte[] bytes = exchange.getRequestBody().readAllBytes();
        String s = new String(bytes, StandardCharsets.UTF_8);
        if (s.isBlank()) {
            throw new IllegalArgumentException("empty request body");
        }
        return s;
    }

    static void send(HttpExchange exchange, int status, Object payload) throws IOException {
        byte[] data = Json.writePretty(payload).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, data.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(data);
        }
    }
}
