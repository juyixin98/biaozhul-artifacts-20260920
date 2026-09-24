package com.example.smj;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CountDownLatch;

/** One asynchronous join task: request, lifecycle state, stats or error. */
public final class JoinTask {

  public enum Status {
    QUEUED,
    RUNNING,
    SUCCEEDED,
    FAILED,
    CANCELLED
  }

  private final String id;
  private final JoinRequest request;
  private final Instant createdAt = Instant.now();
  private final CountDownLatch done = new CountDownLatch(1);

  volatile Status status = Status.QUEUED;
  volatile boolean cancelRequested;
  volatile String error;
  volatile JoinStats stats;
  volatile Instant startedAt;
  volatile Instant finishedAt;

  JoinTask(String id, JoinRequest request) {
    this.id = id;
    this.request = request;
  }

  public String id() {
    return id;
  }

  public JoinRequest request() {
    return request;
  }

  CancelSignal cancelSignal() {
    return () -> cancelRequested || Thread.currentThread().isInterrupted();
  }

  /** Block until the task reaches a terminal state. */
  public void await() throws InterruptedException {
    done.await();
  }

  void finish() {
    done.countDown();
  }

  public boolean isTerminal() {
    return status == Status.SUCCEEDED || status == Status.FAILED || status == Status.CANCELLED;
  }

  public Map<String, Object> toJson() {
    Map<String, Object> m = new LinkedHashMap<>();
    m.put("taskId", id);
    m.put("status", status.name());
    m.put("cancelRequested", cancelRequested);
    m.put("createdAt", createdAt.toString());
    if (startedAt != null) {
      m.put("startedAt", startedAt.toString());
    }
    if (finishedAt != null) {
      m.put("finishedAt", finishedAt.toString());
    }
    if (error != null) {
      m.put("error", error);
    }
    m.put("request", request.toJson());
    if (stats != null) {
      m.put("stats", stats.toJson());
    }
    return m;
  }
}
