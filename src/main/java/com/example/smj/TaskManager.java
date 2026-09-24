package com.example.smj;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicLong;
import java.util.stream.Stream;

/**
 * Runs join tasks on a fixed worker pool. Cancellation is cooperative: the cancel flag
 * is checked throughout the engine, and on cancel/failure the task's temp directory is
 * removed and any partial output file deleted, while the error message is kept on the
 * task for later inspection.
 */
public final class TaskManager implements AutoCloseable {

  private final ExecutorService pool;
  private final Path tempRoot;
  private final Map<String, JoinTask> tasks = new ConcurrentHashMap<>();
  private final AtomicLong seq = new AtomicLong();

  public TaskManager(int maxConcurrent, Path tempRoot) throws IOException {
    this.pool = Executors.newFixedThreadPool(maxConcurrent);
    this.tempRoot = tempRoot;
    Files.createDirectories(tempRoot);
  }

  public Path tempRoot() {
    return tempRoot;
  }

  public JoinTask submit(JoinRequest req) {
    JoinTask t = new JoinTask("task-" + seq.incrementAndGet(), req);
    tasks.put(t.id(), t);
    pool.execute(() -> runTask(t));
    return t;
  }

  public JoinTask get(String id) {
    return tasks.get(id);
  }

  public List<JoinTask> list() {
    return new ArrayList<>(tasks.values());
  }

  /** @return the task, or null if unknown; sets the cancel flag on non-terminal tasks. */
  public JoinTask cancel(String id) {
    JoinTask t = tasks.get(id);
    if (t != null && !t.isTerminal()) {
      t.cancelRequested = true;
    }
    return t;
  }

  private void runTask(JoinTask t) {
    t.status = JoinTask.Status.RUNNING;
    t.startedAt = Instant.now();
    Path dir = tempRoot.resolve(t.id());
    boolean ok = false;
    try {
      CancelSignal.check(t.cancelSignal());
      t.stats = JoinEngine.run(t.request(), dir, t.cancelSignal());
      t.status = JoinTask.Status.SUCCEEDED;
      ok = true;
    } catch (CancelledException e) {
      t.status = JoinTask.Status.CANCELLED;
      t.error = "cancelled by user";
    } catch (Exception e) {
      t.status = JoinTask.Status.FAILED;
      t.error = e.getClass().getSimpleName() + (e.getMessage() != null ? ": " + e.getMessage() : "");
    } finally {
      if (!ok) {
        // Do not leave a misleading partial result behind.
        try {
          Files.deleteIfExists(Path.of(t.request().outputPath()));
        } catch (Exception ignored) {
          // best effort
        }
      }
      deleteRecursively(dir);
      t.finishedAt = Instant.now();
      t.finish();
    }
  }

  static void deleteRecursively(Path dir) {
    if (!Files.exists(dir)) {
      return;
    }
    try (Stream<Path> walk = Files.walk(dir)) {
      walk.sorted(Comparator.reverseOrder())
          .forEach(p -> {
            try {
              Files.deleteIfExists(p);
            } catch (IOException ignored) {
              // best effort
            }
          });
    } catch (IOException ignored) {
      // best effort
    }
  }

  @Override
  public void close() {
    pool.shutdownNow();
  }
}
