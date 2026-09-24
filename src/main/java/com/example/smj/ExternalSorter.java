package com.example.smj;

import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.PriorityQueue;

/**
 * External sort of a JSONL table by its integer key.
 *
 * <p>Rows are accumulated until the configured memory budget (in {@link Row#weight()} units)
 * is exhausted, sorted in memory, and spilled to a run file. If more than
 * {@link #MERGE_FAN_IN} runs are produced, runs are merged in passes so the final cursor
 * never merges more than {@link #MERGE_FAN_IN} files at once. Rows with NULL keys are
 * dropped during the scan (they can never join).
 */
public final class ExternalSorter {

  /** Maximum number of runs merged in a single pass / by the final cursor. */
  public static final int MERGE_FAN_IN = 32;

  private ExternalSorter() {}

  public static SortResult sort(
      Path input, String keyField, long budgetBytes, Path spillDir, String prefix,
      CancelSignal signal) throws IOException {
    Files.createDirectories(spillDir);
    List<Path> runs = new ArrayList<>();
    List<Path> allCreated = new ArrayList<>(); // for cleanup on failure
    List<Row> chunk = new ArrayList<>();
    long chunkBytes = 0;
    long rowCount = 0;
    long nullDropped = 0;
    long spillBytes = 0;
    boolean success = false;
    try {
      try (BufferedReader br = Files.newBufferedReader(input, StandardCharsets.UTF_8)) {
        String line;
        while ((line = br.readLine()) != null) {
          CancelSignal.check(signal);
          String t = line.trim();
          if (t.isEmpty()) {
            continue;
          }
          Row row = RowParser.parse(t, keyField);
          if (row == null) {
            nullDropped++;
            continue;
          }
          rowCount++;
          chunk.add(row);
          chunkBytes += row.weight();
          if (chunkBytes >= budgetBytes) {
            spillBytes += spillChunk(chunk, runs, allCreated, spillDir, prefix, signal);
            chunk = new ArrayList<>();
            chunkBytes = 0;
          }
        }
      }
      if (runs.isEmpty()) {
        chunk.sort(Row.BY_KEY);
        success = true;
        return new SortResult(List.of(), chunk, rowCount, nullDropped, 0);
      }
      if (!chunk.isEmpty()) {
        spillBytes += spillChunk(chunk, runs, allCreated, spillDir, prefix, signal);
      }
      // Merge passes keep the number of simultaneously open files bounded.
      while (runs.size() > MERGE_FAN_IN) {
        CancelSignal.check(signal);
        List<Path> next = new ArrayList<>();
        for (int i = 0; i < runs.size(); i += MERGE_FAN_IN) {
          List<Path> group = runs.subList(i, Math.min(i + MERGE_FAN_IN, runs.size()));
          if (group.size() == 1) {
            next.add(group.get(0));
            continue;
          }
          Path merged = mergeRuns(group, spillDir, prefix, signal, allCreated);
          spillBytes += Files.size(merged);
          next.add(merged);
        }
        runs = next;
      }
      success = true;
      return new SortResult(runs, null, rowCount, nullDropped, spillBytes);
    } finally {
      if (!success) {
        for (Path p : allCreated) {
          deleteQuietly(p);
        }
      }
    }
  }

  private static long spillChunk(
      List<Row> chunk, List<Path> runs, List<Path> allCreated, Path dir, String prefix,
      CancelSignal signal) throws IOException {
    chunk.sort(Row.BY_KEY);
    Path out = Files.createTempFile(dir, prefix + "run-", ".run");
    allCreated.add(out); // register before writing so cancellation still cleans it up
    try (BufferedWriter w = Files.newBufferedWriter(out, StandardCharsets.UTF_8)) {
      for (Row r : chunk) {
        CancelSignal.check(signal);
        SpillFile.writeRow(w, r);
      }
    }
    runs.add(out);
    return Files.size(out);
  }

  /** Merge one group of runs into a single run file; inputs are deleted afterwards. */
  private static Path mergeRuns(List<Path> group, Path dir, String prefix, CancelSignal signal,
      List<Path> allCreated) throws IOException {
    Path out = Files.createTempFile(dir, prefix + "merge-", ".run");
    allCreated.add(out); // register before writing so cancellation still cleans it up
    MergeCursor mc = new MergeCursor(group, signal);
    try (BufferedWriter w = Files.newBufferedWriter(out, StandardCharsets.UTF_8)) {
      while (mc.hasNext()) {
        CancelSignal.check(signal);
        SpillFile.writeRow(w, mc.peek());
        mc.advance();
      }
    } finally {
      mc.close();
    }
    for (Path p : group) {
      deleteQuietly(p);
    }
    return out;
  }

  static void deleteQuietly(Path p) {
    try {
      Files.deleteIfExists(p);
    } catch (IOException ignored) {
      // best effort
    }
  }

  /** Cursor over an in-memory sorted list (no spill happened). */
  static final class MemoryCursor implements SortedCursor {
    private final List<Row> rows;
    private int idx;

    MemoryCursor(List<Row> rows) {
      this.rows = rows;
    }

    @Override
    public boolean hasNext() {
      return idx < rows.size();
    }

    @Override
    public Row peek() {
      return rows.get(idx);
    }

    @Override
    public void advance() {
      idx++;
    }

    @Override
    public void close() {}
  }

  /** K-way merge cursor over sorted run files, using a priority queue. */
  static final class MergeCursor implements SortedCursor {
    private final PriorityQueue<FileCursor> pq =
        new PriorityQueue<>((a, b) -> Long.compare(a.current.key(), b.current.key()));
    private final CancelSignal signal;

    MergeCursor(List<Path> runs, CancelSignal signal) throws IOException {
      this.signal = signal;
      try {
        for (Path p : runs) {
          FileCursor c = new FileCursor(p);
          if (c.current != null) {
            pq.add(c);
          } else {
            c.closeQuietly();
          }
        }
      } catch (IOException | RuntimeException e) {
        close();
        throw e;
      }
    }

    @Override
    public boolean hasNext() {
      return !pq.isEmpty();
    }

    @Override
    public Row peek() {
      return pq.peek().current;
    }

    @Override
    public void advance() throws IOException {
      CancelSignal.check(signal);
      FileCursor c = pq.poll();
      c.readNext();
      if (c.current != null) {
        pq.add(c);
      } else {
        c.closeQuietly();
      }
    }

    @Override
    public void close() {
      for (FileCursor c : pq) {
        c.closeQuietly();
      }
      pq.clear();
    }
  }

  /** One open run file with a one-row lookahead. */
  static final class FileCursor {
    final BufferedReader reader;
    Row current;

    FileCursor(Path path) throws IOException {
      this.reader = Files.newBufferedReader(path, StandardCharsets.UTF_8);
      readNext();
    }

    void readNext() throws IOException {
      current = SpillFile.readRow(reader);
    }

    void closeQuietly() {
      try {
        reader.close();
      } catch (IOException ignored) {
        // best effort
      }
    }
  }
}
