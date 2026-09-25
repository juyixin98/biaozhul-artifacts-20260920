package dev.timeprecision.http;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import dev.timeprecision.ConversionException;
import dev.timeprecision.DecimalSecondsFormatter;
import dev.timeprecision.DecimalSecondsParser;
import dev.timeprecision.ErrorCode;
import dev.timeprecision.TimeConverter;
import dev.timeprecision.json.JsonParseException;
import dev.timeprecision.json.JsonParser;
import dev.timeprecision.json.JsonValue;
import dev.timeprecision.json.JsonWriter;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * Dependency-free HTTP front end exposing the conversion operations as JSON
 * endpoints: POST /convert, POST /parse, POST /format, GET /meta.
 */
public final class TimePrecisionServer {

    private static final int MAX_BODY_BYTES = 64 * 1024;

    private final HttpServer server;

    private TimePrecisionServer(HttpServer server) {
        this.server = server;
    }

    public static TimePrecisionServer start(int port) throws IOException {
        HttpServer http = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        TimePrecisionServer wrapper = new TimePrecisionServer(http);
        http.createContext("/convert", wrapper::handleConvert);
        http.createContext("/parse", wrapper::handleParse);
        http.createContext("/format", wrapper::handleFormat);
        http.createContext("/meta", wrapper::handleMeta);
        http.setExecutor(Executors.newFixedThreadPool(4));
        http.start();
        return wrapper;
    }

    public int port() {
        return server.getAddress().getPort();
    }

    public void stop() {
        server.stop(0);
    }

    private void handleConvert(HttpExchange exchange) throws IOException {
        if (!requirePost(exchange)) {
            return;
        }
        try {
            RequestCodec.ConvertParams p = RequestCodec.decodeConvert(readJson(exchange));
            long result = TimeConverter.convert(p.value(), p.from(), p.to(), p.rounding());
            Map<String, JsonValue> payload = new LinkedHashMap<>();
            payload.put("result", new JsonValue.Str(Long.toString(result)));
            payload.put("resultUnit", new JsonValue.Str(p.to().name()));
            respond(exchange, 200, Responses.ok(payload));
        } catch (ConversionException e) {
            respond(exchange, statusFor(e.code()), Responses.error(e));
        } catch (JsonParseException e) {
            respond(exchange, 400, Responses.error(ErrorCode.BAD_REQUEST, e.getMessage()));
        }
    }

    private void handleParse(HttpExchange exchange) throws IOException {
        if (!requirePost(exchange)) {
            return;
        }
        try {
            RequestCodec.ParseParams p = RequestCodec.decodeParse(readJson(exchange));
            long result = DecimalSecondsParser.parse(p.text(), p.to(), p.rounding());
            Map<String, JsonValue> payload = new LinkedHashMap<>();
            payload.put("result", new JsonValue.Str(Long.toString(result)));
            payload.put("resultUnit", new JsonValue.Str(p.to().name()));
            respond(exchange, 200, Responses.ok(payload));
        } catch (ConversionException e) {
            respond(exchange, statusFor(e.code()), Responses.error(e));
        } catch (JsonParseException e) {
            respond(exchange, 400, Responses.error(ErrorCode.BAD_REQUEST, e.getMessage()));
        }
    }

    private void handleFormat(HttpExchange exchange) throws IOException {
        if (!requirePost(exchange)) {
            return;
        }
        try {
            RequestCodec.FormatParams p = RequestCodec.decodeFormat(readJson(exchange));
            String text = DecimalSecondsFormatter.format(p.value(), p.unit());
            Map<String, JsonValue> payload = new LinkedHashMap<>();
            payload.put("text", new JsonValue.Str(text));
            respond(exchange, 200, Responses.ok(payload));
        } catch (ConversionException e) {
            respond(exchange, statusFor(e.code()), Responses.error(e));
        } catch (JsonParseException e) {
            respond(exchange, 400, Responses.error(ErrorCode.BAD_REQUEST, e.getMessage()));
        }
    }

    private void handleMeta(HttpExchange exchange) throws IOException {
        if (!"GET".equalsIgnoreCase(exchange.getRequestMethod())) {
            respond(exchange, 405, Responses.error(ErrorCode.METHOD_NOT_ALLOWED, "use GET"));
            return;
        }
        respond(exchange, 200, Responses.ok(Map.of()));
    }

    private boolean requirePost(HttpExchange exchange) throws IOException {
        if ("POST".equalsIgnoreCase(exchange.getRequestMethod())) {
            return true;
        }
        respond(exchange, 405, Responses.error(ErrorCode.METHOD_NOT_ALLOWED, "use POST"));
        return false;
    }

    private static JsonValue readJson(HttpExchange exchange) throws IOException {
        byte[] body = readBodyCapped(exchange.getRequestBody());
        return JsonParser.parse(new String(body, StandardCharsets.UTF_8));
    }

    private static byte[] readBodyCapped(InputStream in) throws IOException {
        byte[] body = in.readNBytes(MAX_BODY_BYTES + 1);
        if (body.length > MAX_BODY_BYTES) {
            throw new ConversionException(ErrorCode.PAYLOAD_TOO_LARGE,
                    "request body exceeds " + MAX_BODY_BYTES + " bytes");
        }
        return body;
    }

    /** Maps domain error codes to HTTP statuses; unprocessable content defaults to 422. */
    private static int statusFor(ErrorCode code) {
        return switch (code) {
            case BAD_REQUEST -> 400;
            case PAYLOAD_TOO_LARGE -> 413;
            case METHOD_NOT_ALLOWED -> 405;
            case INTERNAL -> 500;
            default -> 422;
        };
    }

    private static void respond(HttpExchange exchange, int status, JsonValue body) throws IOException {        byte[] bytes = JsonWriter.write(body).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, bytes.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(bytes);
        }
    }
}
