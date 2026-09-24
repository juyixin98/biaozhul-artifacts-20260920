package com.example.smj;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/** End-to-end tests against a real HTTP server instance. */
class HttpApiTest {

  @TempDir Path tmp;

  private TaskManager tasks;
  private HttpApi api;
  private String base;
  private final HttpClient client = HttpClient.newHttpClient();

  @BeforeEach
  void startServer() throws IOException {
    tasks = new TaskManager(2, tmp.resolve("temp"));
    api = HttpApi.start(0, tasks, 1 << 20);
    base = "http://127.0.0.1:" + api.port();
  }

  @AfterEach
  void stopServer() {
    api.stop();
    tasks.close();
  }

  private HttpResponse<String> post(String path, String json) throws Exception {
    HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
        .POST(HttpRequest.BodyPublishers.ofString(json))
        .header("Content-Type", "application/json")
        .build();
    return client.send(req, HttpResponse.BodyHandlers.ofString());
  }

  private HttpResponse<String> get(String path) throws Exception {
    return client.send(HttpRequest.newBuilder(URI.create(base + path)).GET().build(),
        HttpResponse.BodyHandlers.ofString());
  }

  @SuppressWarnings("unchecked")
  private static Map<String, Object> asMap(HttpResponse<String> resp) {
    return (Map<String, Object>) Json.parse(resp.body());
  }

  private static long num(Map<String, Object> m, String key) {
    return Long.parseLong(((Json.Num) m.get(key)).raw());
  }

  @Test
  void syncJoinWithHotKeyAndNulls() throws Exception {
    // Hot key 42: 500 x 400 rows of ~100 bytes -> ~50KB/40KB groups >> 4096-byte budget.
    List<String> left = new ArrayList<>(TestUtil.genHotRows(500, 42, 100, "L"));
    left.addAll(TestUtil.genRows(800, 100, 100, 1));
    left.add("{\"k\":null,\"v\":\"null-left\"}");
    List<String> right = new ArrayList<>(TestUtil.genHotRows(400, 42, 100, "R"));
    right.addAll(TestUtil.genRows(800, 100, 100, 2));
    right.add("{\"k\":null,\"v\":\"null-right\"}");
    Path leftPath = tmp.resolve("L.jsonl");
    Path rightPath = tmp.resolve("R.jsonl");
    Path out = tmp.resolve("out.jsonl");
    TestUtil.writeJsonl(leftPath, left);
    TestUtil.writeJsonl(rightPath, right);

    String body = Json.write(Map.of(
        "leftPath", leftPath.toString(),
        "rightPath", rightPath.toString(),
        "keyField", "k",
        "outputPath", out.toString(),
        "memoryBudgetBytes", new Json.Num("4096")));
    HttpResponse<String> resp = post("/join/sync", body);

    assertEquals(200, resp.statusCode(), resp.body());
    Map<String, Object> task = asMap(resp);
    assertEquals("SUCCEEDED", task.get("status"));
    @SuppressWarnings("unchecked")
    Map<String, Object> stats = (Map<String, Object>) task.get("stats");
    assertTrue(num(stats, "spilledKeyGroups") >= 2, "hot groups must spill: " + stats);
    assertTrue(num(stats, "sortSpillWriteBytes") > 0);
    assertTrue(num(stats, "groupSpillWriteBytes") > 0);
    assertEquals(1, num(stats, "leftNullKeysDropped"));
    assertEquals(1, num(stats, "rightNullKeysDropped"));

    List<String> want = TestUtil.naiveJoin(leftPath, rightPath, "k");
    List<String> got = TestUtil.readSorted(out);
    assertEquals(want.size(), num(stats, "outputRows"));
    assertEquals(want, got, "HTTP join output multiset must match the naive reference");
  }

  @Test
  void asyncCancelCleansTempFilesAndKeepsError() throws Exception {
    // Large inputs + tiny budget -> the sort phase runs long enough to cancel.
    Path leftPath = tmp.resolve("bigL.jsonl");
    Path rightPath = tmp.resolve("bigR.jsonl");
    Path out = tmp.resolve("cancelled-out.jsonl");
    TestUtil.writeJsonl(leftPath, TestUtil.genRows(200_000, 10_000, 80, 3));
    TestUtil.writeJsonl(rightPath, TestUtil.genRows(200_000, 10_000, 80, 4));

    String body = Json.write(Map.of(
        "leftPath", leftPath.toString(),
        "rightPath", rightPath.toString(),
        "keyField", "k",
        "outputPath", out.toString(),
        "memoryBudgetBytes", new Json.Num("4096")));
    HttpResponse<String> submit = post("/join", body);
    assertEquals(202, submit.statusCode(), submit.body());
    String taskId = (String) asMap(submit).get("taskId");

    // Wait for RUNNING, then cancel.
    waitForStatus(taskId, "RUNNING");
    HttpResponse<String> cancel = post("/tasks/" + taskId + "/cancel", "");
    assertEquals(200, cancel.statusCode(), cancel.body());
    Map<String, Object> done = waitForTerminal(taskId);

    assertEquals("CANCELLED", done.get("status"), String.valueOf(done));
    assertEquals("cancelled by user", done.get("error"), "error info must be retained");
    assertNotNull(done.get("finishedAt"));
    assertFalse(Files.exists(out), "partial output must be removed after cancel");
    try (var stream = Files.list(tasks.tempRoot())) {
      assertTrue(stream.findAny().isEmpty(), "task temp dir must be removed after cancel");
    }
  }

  @Test
  void badRequestIsRejected() throws Exception {
    HttpResponse<String> resp = post("/join", "{\"leftPath\":\"/nope\"}");
    assertEquals(400, resp.statusCode());
    assertTrue(resp.body().contains("error"));
  }

  @Test
  void nonIntegerKeyFailsTaskAndRetainsError() throws Exception {
    Path leftPath = tmp.resolve("badL.jsonl");
    Path rightPath = tmp.resolve("badR.jsonl");
    TestUtil.writeJsonl(leftPath, List.of("{\"k\":3.5,\"v\":\"x\"}"));
    TestUtil.writeJsonl(rightPath, List.of("{\"k\":3}"));

    String body = Json.write(Map.of(
        "leftPath", leftPath.toString(),
        "rightPath", rightPath.toString(),
        "keyField", "k",
        "outputPath", tmp.resolve("bad-out.jsonl").toString()));
    HttpResponse<String> resp = post("/join/sync", body);

    assertEquals(500, resp.statusCode(), resp.body());
    Map<String, Object> task = asMap(resp);
    assertEquals("FAILED", task.get("status"));
    assertTrue(String.valueOf(task.get("error")).contains("not an integer"),
        String.valueOf(task));
    // Error remains queryable afterwards.
    HttpResponse<String> again = get("/tasks/" + task.get("taskId"));
    assertEquals(200, again.statusCode());
    assertTrue(again.body().contains("not an integer"));
  }

  @Test
  void unknownTaskIs404() throws Exception {
    assertEquals(404, get("/tasks/task-999").statusCode());
  }

  private Map<String, Object> waitForStatus(String taskId, String status) throws Exception {
    long deadline = System.currentTimeMillis() + 30_000;
    while (System.currentTimeMillis() < deadline) {
      Map<String, Object> t = asMap(get("/tasks/" + taskId));
      if (status.equals(t.get("status"))) {
        return t;
      }
      if (t.get("status") != null && isTerminal((String) t.get("status"))) {
        return t; // already past the requested state
      }
      Thread.sleep(20);
    }
    throw new AssertionError("task " + taskId + " did not reach status " + status);
  }

  private Map<String, Object> waitForTerminal(String taskId) throws Exception {
    long deadline = System.currentTimeMillis() + 30_000;
    while (System.currentTimeMillis() < deadline) {
      Map<String, Object> t = asMap(get("/tasks/" + taskId));
      if (isTerminal((String) t.get("status"))) {
        return t;
      }
      Thread.sleep(20);
    }
    throw new AssertionError("task " + taskId + " did not terminate");
  }

  private static boolean isTerminal(String status) {
    return "SUCCEEDED".equals(status) || "FAILED".equals(status) || "CANCELLED".equals(status);
  }
}
