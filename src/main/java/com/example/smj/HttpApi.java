package com.example.smj;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * HTTP API on the JDK's built-in {@link HttpServer}.
 *
 * <ul>
 *   <li>{@code GET  /health} — liveness probe
 *   <li>{@code POST /join} — submit an async join, returns 202 + task id
 *   <li>{@code POST /join/sync} — submit and wait for completion
 *   <li>{@code GET  /tasks} — list tasks
 *   <li>{@code GET  /tasks/{id}} — task status, stats and error
 *   <li>{@code POST /tasks/{id}/cancel} — request cancellation
 * </ul>
 */
public final class HttpApi {

  /** Smallest accepted memory budget; below this the engine cannot make progress sanely. */
  public static final long MIN_BUDGET_BYTES = 4096;

  private final HttpServer server;
  private final TaskManager tasks;
  private final long defaultBudgetBytes;

  private HttpApi(HttpServer server, TaskManager tasks, long defaultBudgetBytes) {
    this.server = server;
    this.tasks = tasks;
    this.defaultBudgetBytes = defaultBudgetBytes;
  }

  public static HttpApi start(int port, TaskManager tasks, long defaultBudgetBytes)
      throws IOException {
    HttpServer server = HttpServer.create(new InetSocketAddress(port), 64);
    HttpApi api = new HttpApi(server, tasks, defaultBudgetBytes);
    server.createContext("/health", api::handleHealth);
    server.createContext("/join/sync", api::handleJoinSync);
    server.createContext("/join", api::handleJoin);
    server.createContext("/tasks", api::handleTasks);
    server.setExecutor(Executors.newCachedThreadPool());
    server.start();
    return api;
  }

  public int port() {
    return server.getAddress().getPort();
  }

  public void stop() {
    server.stop(0);
  }

  private void handleHealth(HttpExchange ex) throws IOException {
    if (!requireMethod(ex, "GET")) return;
    send(ex, 200, Map.of("status", "ok"));
  }

  private void handleJoin(HttpExchange ex) throws IOException {
    if (!requireMethod(ex, "POST")) return;
    JoinRequest req;
    try {
      req = parseJoinRequest(body(ex));
    } catch (JoinException e) {
      send(ex, 400, Map.of("error", e.getMessage()));
      return;
    }
    JoinTask t = tasks.submit(req);
    send(ex, 202, t.toJson());
  }

  private void handleJoinSync(HttpExchange ex) throws IOException {
    if (!requireMethod(ex, "POST")) return;
    JoinRequest req;
    try {
      req = parseJoinRequest(body(ex));
    } catch (JoinException e) {
      send(ex, 400, Map.of("error", e.getMessage()));
      return;
    }
    JoinTask t = tasks.submit(req);
    try {
      t.await();
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
      send(ex, 500, Map.of("error", "interrupted while waiting", "taskId", t.id()));
      return;
    }
    send(ex, t.status == JoinTask.Status.SUCCEEDED ? 200 : 500, t.toJson());
  }

  private void handleTasks(HttpExchange ex) throws IOException {
    String[] parts = ex.getRequestURI().getPath().split("/");
    // parts: ["", "tasks", id?, "cancel"?]
    if (parts.length == 2) {
      if (!requireMethod(ex, "GET")) return;
      send(ex, 200, tasks.list().stream().map(JoinTask::toJson).toList());
      return;
    }
    JoinTask t = tasks.get(parts[2]);
    if (t == null) {
      send(ex, 404, Map.of("error", "unknown task: " + parts[2]));
      return;
    }
    if (parts.length == 3) {
      if (!requireMethod(ex, "GET")) return;
      send(ex, 200, t.toJson());
      return;
    }
    if (parts.length == 4 && parts[3].equals("cancel")) {
      if (!requireMethod(ex, "POST")) return;
      if (t.isTerminal()) {
        send(ex, 409, Map.of("error", "task already in terminal state", "status",
            t.status.name()));
        return;
      }
      tasks.cancel(t.id());
      send(ex, 200, t.toJson());
      return;
    }
    send(ex, 404, Map.of("error", "not found"));
  }

  private JoinRequest parseJoinRequest(String bodyText) {
    Object v;
    try {
      v = Json.parse(bodyText);
    } catch (Json.JsonException e) {
      throw new JoinException("invalid JSON body: " + e.getMessage());
    }
    if (!(v instanceof Map)) {
      throw new JoinException("body must be a JSON object");
    }
    Map<?, ?> m = (Map<?, ?>) v;
    String left = reqString(m, "leftPath");
    String right = reqString(m, "rightPath");
    String keyField = reqString(m, "keyField");
    String output = reqString(m, "outputPath");
    long budget = defaultBudgetBytes;
    Object b = m.get("memoryBudgetBytes");
    if (b != null) {
      if (!(b instanceof Json.Num n) || !n.raw().matches("-?\\d+")) {
        throw new JoinException("memoryBudgetBytes must be an integer");
      }
      budget = Long.parseLong(n.raw());
      if (budget < MIN_BUDGET_BYTES) {
        throw new JoinException("memoryBudgetBytes must be >= " + MIN_BUDGET_BYTES);
      }
    }
    if (!Files.isRegularFile(Path.of(left))) {
      throw new JoinException("leftPath is not a readable file: " + left);
    }
    if (!Files.isRegularFile(Path.of(right))) {
      throw new JoinException("rightPath is not a readable file: " + right);
    }
    return new JoinRequest(left, right, keyField, output, budget);
  }

  private static String reqString(Map<?, ?> m, String name) {
    Object v = m.get(name);
    if (!(v instanceof String s) || s.isEmpty()) {
      throw new JoinException("missing or invalid required field: " + name);
    }
    return s;
  }

  private static String body(HttpExchange ex) throws IOException {
    return new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
  }

  private static boolean requireMethod(HttpExchange ex, String method) throws IOException {
    if (ex.getRequestMethod().equalsIgnoreCase(method)) {
      return true;
    }
    send(ex, 405, Map.of("error", "method not allowed, use " + method));
    return false;
  }

  private static void send(HttpExchange ex, int status, Object json) throws IOException {
    byte[] body = Json.write(json).getBytes(StandardCharsets.UTF_8);
    ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
    ex.sendResponseHeaders(status, body.length);
    try (OutputStream os = ex.getResponseBody()) {
      os.write(body);
    }
  }
}
