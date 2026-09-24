package com.example.smj;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class ExternalSorterTest {

  @TempDir Path tmp;

  @Test
  void sortsWithSpillAndDropsNullKeys() throws IOException {
    List<String> rows = new ArrayList<>(TestUtil.genRows(5000, 200, 90, 42));
    for (int i = 0; i < rows.size(); i += 37) {
      rows.set(i, "{\"k\":null,\"v\":\"null" + i + "\"}");
    }
    Path input = tmp.resolve("in.jsonl");
    TestUtil.writeJsonl(input, rows);
    Path spillDir = tmp.resolve("spill");

    SortResult r = ExternalSorter.sort(input, "k", 2048, spillDir, "T", CancelSignal.NONE);

    assertTrue(r.runCount() > 1, "expected multiple spill runs, got " + r.runCount());
    assertTrue(r.spillWriteBytes() > 0);
    assertEquals(countNulls(rows), r.nullKeysDropped());
    assertEquals(5000 - countNulls(rows), r.rowCount());

    List<String> expected = new ArrayList<>();
    long prev = Long.MIN_VALUE;
    try (SortedCursor c = r.openCursor(CancelSignal.NONE)) {
      while (c.hasNext()) {
        assertTrue(c.peek().key() >= prev, "output must be key-sorted");
        prev = c.peek().key();
        expected.add(c.peek().json());
        c.advance();
      }
    }
    List<String> want = new ArrayList<>();
    for (String line : rows) {
      if (!line.contains("\"k\":null")) want.add(line);
    }
    expected.sort(String::compareTo);
    want.sort(String::compareTo);
    assertEquals(want, expected, "sorted output must equal input multiset minus NULL keys");

    r.deleteRuns();
    assertTrue(Files.list(spillDir).findAny().isEmpty(), "run files must be deleted");
  }

  @Test
  void staysInMemoryWhenBudgetSuffices() throws IOException {
    List<String> rows = TestUtil.genRows(500, 100, 60, 7);
    Path input = tmp.resolve("small.jsonl");
    TestUtil.writeJsonl(input, rows);

    SortResult r = ExternalSorter.sort(input, "k", 1L << 30, tmp.resolve("spill2"), "T",
        CancelSignal.NONE);

    assertEquals(0, r.runCount());
    assertEquals(0, r.spillWriteBytes());
    long prev = Long.MIN_VALUE;
    try (SortedCursor c = r.openCursor(CancelSignal.NONE)) {
      while (c.hasNext()) {
        assertTrue(c.peek().key() >= prev);
        prev = c.peek().key();
        c.advance();
      }
    }
  }

  @Test
  void manyRunsTriggerMergePasses() throws IOException {
    // ~120-byte rows with a 1024-byte budget -> ~8 rows per run -> ~375 runs > MERGE_FAN_IN.
    List<String> rows = TestUtil.genRows(3000, 5000, 120, 99);
    Path input = tmp.resolve("many.jsonl");
    TestUtil.writeJsonl(input, rows);
    Path spillDir = tmp.resolve("spill3");

    SortResult r = ExternalSorter.sort(input, "k", 1024, spillDir, "T", CancelSignal.NONE);

    assertTrue(r.runCount() > 1);
    assertTrue(r.runCount() <= ExternalSorter.MERGE_FAN_IN,
        "merge passes must bound final run count, got " + r.runCount());
    List<String> got = new ArrayList<>();
    long prev = Long.MIN_VALUE;
    try (SortedCursor c = r.openCursor(CancelSignal.NONE)) {
      while (c.hasNext()) {
        assertTrue(c.peek().key() >= prev);
        prev = c.peek().key();
        got.add(c.peek().json());
        c.advance();
      }
    }
    List<String> want = new ArrayList<>(rows);
    got.sort(String::compareTo);
    want.sort(String::compareTo);
    assertEquals(want, got);
    r.deleteRuns();
  }

  @Test
  void cancelDuringSortCleansUpSpillFiles() throws IOException {
    List<String> rows = TestUtil.genRows(5000, 100, 120, 5);
    Path input = tmp.resolve("cancel.jsonl");
    TestUtil.writeJsonl(input, rows);
    Path spillDir = tmp.resolve("spill4");
    AtomicInteger checks = new AtomicInteger();
    CancelSignal tripAfter500 = () -> checks.incrementAndGet() > 500;

    assertThrows(CancelledException.class,
        () -> ExternalSorter.sort(input, "k", 1024, spillDir, "T", tripAfter500));

    if (Files.exists(spillDir)) {
      assertTrue(Files.list(spillDir).findAny().isEmpty(),
          "spill files must be removed after cancellation");
    }
  }

  @Test
  void rejectsNonIntegerKey() throws IOException {
    Path input = tmp.resolve("bad.jsonl");
    TestUtil.writeJsonl(input, List.of("{\"k\":3.5}", "{\"k\":1}"));
    JoinException e = assertThrows(JoinException.class, () ->
        ExternalSorter.sort(input, "k", 1 << 20, tmp.resolve("spill5"), "T", CancelSignal.NONE));
    assertTrue(e.getMessage().contains("not an integer"));
  }

  private static long countNulls(List<String> rows) {
    return rows.stream().filter(l -> l.contains("\"k\":null")).count();
  }
}
