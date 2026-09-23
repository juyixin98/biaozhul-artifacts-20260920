package join;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.Reader;
import java.io.InputStreamReader;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * HTTP front end (JDK built-in {@link HttpServer}, no dependencies).
 *
 * Routes:
 *   GET  /health                       liveness probe
 *   POST /join                         submit a job {leftInput,rightInput,leftKeyColumn,
 *                                      rightKeyColumn,memoryBudgetBytes?}
 *   GET  /join                         list jobs
 *   GET  /join/{id}                    job status (error text retained after failure/cancel)
 *   POST /join/{id}/cancel             request cancellation
 *   GET  /join/{id}/result             download result JSONL (200 only when COMPLETED)
 */
public final class ApiServer {

    private final HttpServer server;
    private final JobService jobs;

    ApiServer(int port, Path dataDir, int concurrency) throws IOException {
        this.jobs = new JobService(dataDir, concurrency);
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        this.server.createContext("/", new RootHandler());
        this.server.setExecutor(Executors.newFixedThreadPool(Math.max(4, concurrency * 2),
                r -> {
                    Thread t = new Thread(r, "http-" + r.hashCode());
                    t.setDaemon(true);
                    return t;
                }));
    }

    void start() {
        server.start();
    }

    int boundPort() {
        return server.getAddress().getPort();
    }

    void stop() {
        jobs.shutdown();
        server.stop(1);
    }

    public static void main(String[] args) throws Exception {
        int port = intProp("port", "JOIN_PORT", 8080);
        int concurrency = intProp("concurrency", "JOIN_CONCURRENCY", 2);
        String dir = System.getProperty("dataDir", System.getenv().getOrDefault("JOIN_DATA_DIR", "./data"));

        ApiServer api = new ApiServer(port, Path.of(dir), concurrency);
        api.start();
        System.out.println("external-sort merge-join service listening on http://localhost:"
                + api.boundPort() + "  dataDir=" + Path.of(dir).toAbsolutePath());
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("shutting down...");
            api.stop();
        }));
    }

    private static int intProp(String prop, String env, int def) {
        String v = System.getProperty(prop, System.getenv().getOrDefault(env, String.valueOf(def)));
        try {
            int n = Integer.parseInt(v.trim());
            if (n < 1) {
                throw new NumberFormatException();
            }
            return n;
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("Bad integer for " + prop + ": " + v);
        }
    }

    // -------------------------------------------------------------- handlers

    final class RootHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange ex) throws IOException {
            try {
                route(ex);
            } catch (Exception e) {
                if (ex.getResponseCode() == -1) {
                    sendError(ex, 500, "InternalServerError", String.valueOf(e.getMessage()));
                }
            }
        }

        private void route(HttpExchange ex) throws IOException {
            String method = ex.getRequestMethod();
            String path = ex.getRequestURI().getPath();

            if (method.equals("GET") && path.equals("/health")) {
                Map<String, Object> b = new LinkedHashMap<>();
                b.put("status", "ok");
                sendJson(ex, 200, b);
                return;
            }
            if (path.equals("/join") && method.equals("POST")) {
                handleSubmit(ex);
                return;
            }
            if (path.equals("/join") && method.equals("GET")) {
                List<Object> all = new java.util.ArrayList<>();
                for (Job j : jobs.list()) {
                    all.add(j.describe());
                }
                Map<String, Object> b = new LinkedHashMap<>();
                b.put("jobs", all);
                sendJson(ex, 200, b);
                return;
            }
            String[] parts = path.split("/");
            // ["", "join", "{id}"] or ["", "join", "{id}", "cancel"|"result"]
            if (parts.length >= 3 && parts[1].equals("join") && !parts[2].isEmpty()) {
                String id = parts[2];
                Job job = jobs.get(id);
                if (job == null) {
                    sendError(ex, 404, "NotFound", "no such job: " + id);
                    return;
                }
                if (parts.length == 3 && method.equals("GET")) {
                    sendJson(ex, 200, job.describe());
                    return;
                }
                if (parts.length == 4 && parts[3].equals("cancel") && method.equals("POST")) {
                    boolean ok = jobs.cancel(id);
                    if (!ok) {
                        sendError(ex, 409, "AlreadyTerminal",
                                "job " + id + " is already in a terminal state");
                        return;
                    }
                    Map<String, Object> b = new LinkedHashMap<>();
                    b.put("cancelled", true);
                    b.put("job", jobs.get(id).describe());
                    sendJson(ex, 202, b);
                    return;
                }
                if (parts.length == 4 && parts[3].equals("result") && method.equals("GET")) {
                    handleResult(ex, job);
                    return;
                }
            }
            sendError(ex, 404, "NotFound", "no route for " + method + " " + path);
        }

        private void handleSubmit(HttpExchange ex) throws IOException {
            try (InputStream in = ex.getRequestBody();
                 Reader reader = new InputStreamReader(in, StandardCharsets.UTF_8)) {
                Job job = jobs.submit(reader);
                sendJson(ex, 202, job.describe());
            } catch (JobService.BadRequestException e) {
                sendError(ex, 400, "BadRequest", e.getMessage());
            }
        }

        private void handleResult(HttpExchange ex, Job job) throws IOException {
            switch (job.status()) {
                case COMPLETED:
                    break;
                case FAILED:
                case CANCELLED:
                    sendError(ex, 410, job.status().name(),
                            job.error() == null ? job.status().name() : job.error());
                    return;
                default:
                    sendError(ex, 409, "NotReady", "job is " + job.status()
                            + "; poll GET /join/" + job.id);
                    return;
            }
            Path file = job.outputFile;
            if (!Files.isRegularFile(file)) {
                sendError(ex, 410, "Gone", "result file no longer exists");
                return;
            }
            long len = Files.size(file);
            Headers h = ex.getResponseHeaders();
            h.set("Content-Type", "application/x-ndjson; charset=utf-8");
            h.set("Content-Disposition", "attachment; filename=\"" + job.id + ".jsonl\"");
            ex.sendResponseHeaders(200, len);
            try (OutputStream out = ex.getResponseBody();
                 InputStream in = Files.newInputStream(file)) {
                in.transferTo(out);
            }
        }
    }

    // ------------------------------------------------------------------ io

    static void sendJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] data = Json.stringifyBytes(body);
        Headers h = ex.getResponseHeaders();
        h.set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, data.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(data);
        }
    }

    static void sendError(HttpExchange ex, int status, String type, String message) throws IOException {
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("error", type);
        b.put("message", message);
        sendJson(ex, status, b);
    }
}
