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

class MergeJoinTest {

  @TempDir Path tmp;

  private JoinStats runJoin(List<String> leftRows, List<String> rightRows, long budget,
      Path out, CancelSignal signal) throws IOException {
    Path left = tmp.resolve("left.jsonl");
    Path right = tmp.resolve("right.jsonl");
    TestUtil.writeJsonl(left, leftRows);
    TestUtil.writeJsonl(right, rightRows);
    JoinRequest req = new JoinRequest(
        left.toString(), right.toString(), "k", out.toString(), budget);
    return JoinEngine.run(req, tmp.resolve("work"), signal);
  }

  @Test
  void basicJoinWithDuplicatesAndNulls() throws IOException {
    List<String> left = List.of(
        "{\"k\":1,\"v\":\"a\"}",
        "{\"k\":1,\"v\":\"b\"}",
        "{\"k\":2,\"v\":\"c\"}",
        "{\"k\":null,\"v\":\"null-left\"}",
        "{\"k\":5,\"v\":\"no-match\"}");
    List<String> right = List.of(
        "{\"k\":1,\"v\":\"x\"}",
        "{\"k\":1,\"v\":\"y\"}",
        "{\"k\":1,\"v\":\"z\"}",
        "{\"k\":null,\"v\":\"null-right\"}",
        "{\"k\":3,\"v\":\"no-match\"}");
    Path out = tmp.resolve("out.jsonl");

    JoinStats stats = runJoin(left, right, 1 << 20, out, CancelSignal.NONE);

    // key=1: 2 left x 3 right = 6 rows; NULLs and non-matching keys produce nothing.
    assertEquals(6, stats.outputRows);
    assertEquals(1, stats.matchedKeyGroups);
    assertEquals(1, stats.leftNullKeysDropped);
    assertEquals(1, stats.rightNullKeysDropped);
    assertEquals(0, stats.spilledKeyGroups);
    List<String> got = TestUtil.readSorted(out);
    List<String> want = TestUtil.naiveJoin(tmp.resolve("left.jsonl"), tmp.resolve("right.jsonl"), "k");
    assertEquals(want, got);
  }

  @Test
  void hotKeyGroupLargerThanMemoryBudget() throws IOException {
    // Hot key 7: 300 left rows x 250 right rows, ~100 bytes each -> ~30KB/25KB groups,
    // far above the 4096-byte budget (2048 per side). Both groups must spill and the
    // block nested-loop path must produce the full 75000-row cartesian product.
    List<String> left = new ArrayList<>(TestUtil.genHotRows(300, 7, 100, "L"));
    left.addAll(TestUtil.genRows(500, 50, 100, 11));
    left.add("{\"k\":null,\"v\":\"null-left\"}");
    List<String> right = new ArrayList<>(TestUtil.genHotRows(250, 7, 100, "R"));
    right.addAll(TestUtil.genRows(500, 50, 100, 22));
    right.add("{\"k\":null,\"v\":\"null-right\"}");
    Path out = tmp.resolve("hot-out.jsonl");

    JoinStats stats = runJoin(left, right, 4096, out, CancelSignal.NONE);

    List<String> want = TestUtil.naiveJoin(tmp.resolve("left.jsonl"), tmp.resolve("right.jsonl"), "k");
    List<String> got = TestUtil.readSorted(out);
    assertEquals(want.size(), stats.outputRows);
    assertEquals(want, got, "join output multiset must match the naive reference");
    assertTrue(stats.spilledKeyGroups >= 2,
        "both hot groups must spill, got " + stats.spilledKeyGroups);
    assertTrue(stats.groupSpillWriteBytes > 0);
    assertTrue(stats.sortSpillWriteBytes > 0, "sort phase must spill under this budget");
    assertTrue(stats.leftRuns > 1 && stats.rightRuns > 1);
    assertEquals(1, stats.leftNullKeysDropped);
    assertEquals(1, stats.rightNullKeysDropped);
    // Temp files must be gone after the run.
    assertTrue(Files.list(tmp.resolve("work")).findAny().isEmpty(),
        "no temp files may remain after a successful join");
  }

  @Test
  void cancelDuringJoinCleansUpTempFiles() throws IOException {
    // One giant shared key -> the join emits rows for a long time; cancel mid-emit.
    List<String> left = TestUtil.genHotRows(2000, 9, 100, "L");
    List<String> right = TestUtil.genHotRows(2000, 9, 100, "R");
    Path out = tmp.resolve("cancel-out.jsonl");
    AtomicInteger checks = new AtomicInteger();
    CancelSignal tripAfter20k = () -> checks.incrementAndGet() > 20_000;

    assertThrows(CancelledException.class,
        () -> runJoin(left, right, 4096, out, tripAfter20k));

    assertTrue(Files.list(tmp.resolve("work")).findAny().isEmpty(),
        "group spill files must be removed after cancellation");
  }

  @Test
  void missingKeyFieldFails() {
    List<String> left = List.of("{\"other\":1}");
    List<String> right = List.of("{\"k\":1}");
    JoinException e = assertThrows(JoinException.class,
        () -> runJoin(left, right, 1 << 20, tmp.resolve("x.jsonl"), CancelSignal.NONE));
    assertTrue(e.getMessage().contains("missing key field"));
  }
  }
