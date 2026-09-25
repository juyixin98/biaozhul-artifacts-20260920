package com.timeconv.http;

import com.timeconv.ConvertException;
import com.timeconv.ConvertService;
import com.timeconv.ErrorCode;
import com.timeconv.json.Json;
import com.timeconv.json.JsonParseException;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.Map;

/**
 * JSON-over-HTTP API built on the JDK's embedded HTTP server. No external dependencies.
 *
 * <ul>
 *   <li>POST /convert — integer timestamp unit conversion</li>
 *   <li>POST /parse   — fractional/ISO-8601 text to integer timestamp</li>
 *   <li>GET  /meta    — capabilities and tzdb version</li>
 * </ul>
 */
public final class Server implements AutoCloseable {

    private final HttpServer server;
    private final ConvertService service = new ConvertService();

    public Server(int port) {
        try {
            server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        } catch (IOException e) {
            throw new UncheckedIOException("cannot bind HTTP server", e);
        }
        server.createContext("/convert", this::handleConvert);
        server.createContext("/parse", this::handleParse);
        server.createContext("/meta", this::handleMeta);
        server.setExecutor(null);
    }

    public void start() {
        server.start();
    }

    public int port() {
        return server.getAddress().getPort();
    }

    @Override
    public void close() {
        server.stop(0);
    }

    private void handleConvert(HttpExchange ex) throws IOException {
        handlePost(ex, service::convert);
    }

    private void handleParse(HttpExchange ex) throws IOException {
        handlePost(ex, service::parse);
    }

    private void handleMeta(HttpExchange ex) throws IOException {
        if (!"GET".equalsIgnoreCase(ex.getRequestMethod())) {
            send(ex, 405, ConvertService.error(ErrorCode.METHOD_NOT_ALLOWED, "use GET"));
            return;
        }
        send(ex, 200, service.meta());
    }

    private void handlePost(HttpExchange ex, java.util.function.Function<Map<String, Object>, Map<String, Object>> handler)
            throws IOException {
        if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
            send(ex, 405, ConvertService.error(ErrorCode.METHOD_NOT_ALLOWED, "use POST"));
            return;
        }
        String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
        Map<String, Object> response;
        int status;
        try {
            Map<String, Object> request = Json.parseObject(body);
            response = handler.apply(request);
            status = 200;
        } catch (JsonParseException e) {
            response = ConvertService.error(ErrorCode.BAD_REQUEST, "invalid JSON: " + e.getMessage());
            status = 400;
        } catch (ConvertException e) {
            response = ConvertService.error(e.code(), e.getMessage());
            status = 400;
        } catch (Exception e) {
            response = ConvertService.error(ErrorCode.INTERNAL, "unexpected error");
            status = 500;
        }
        send(ex, status, response);
    }

    private static void send(HttpExchange ex, int status, Map<String, Object> body) throws IOException {
        byte[] bytes = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        ex.getResponseBody().write(bytes);
        ex.close();
    }

    /** Entry point: {@code java com.timeconv.http.Server [port]} (default port 8080). */
    public static void main(String[] args) {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 8080;
        Server s = new Server(port);
        s.start();
        System.out.println("time-precision-converter listening on http://127.0.0.1:" + s.port());
        System.out.println("tzdb version: " + com.timeconv.TzdbInfo.version());
    }
}
