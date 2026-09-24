package com.example.smj;

import java.io.IOException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * Sort-merge join over two key-sorted cursors.
 *
 * <p>Equal keys form groups on both sides; each group pair produces the cartesian
 * product (duplicate keys match multiplicatively). Each side's group is buffered in a
 * {@link SpillableGroup} with half the memory budget; when a hot key group exceeds its
 * share it spills to disk and the pair is joined with a block nested-loop, so groups
 * of any size are handled within the budget.
 */
public final class MergeJoiner {

  /** Per-join statistics from the merge phase. */
  public record PhaseStats(long matchedKeyGroups, long spilledKeyGroups, long groupSpillWriteBytes,
                           long outputRows) {}

  private MergeJoiner() {}

  public static PhaseStats join(
      SortedCursor left, SortedCursor right, RowSink sink, long budgetBytes, Path groupDir,
      CancelSignal signal) throws IOException {
    long leftBudget = Math.max(1024, budgetBytes / 2);
    long rightBudget = Math.max(1024, budgetBytes - leftBudget);
    long matchedGroups = 0;
    long spilledGroups = 0;
    long groupSpillBytes = 0;
    long outputRows = 0;

    while (left.hasNext() && right.hasNext()) {
      CancelSignal.check(signal);
      long lk = left.peek().key();
      long rk = right.peek().key();
      if (lk < rk) {
        left.advance();
        continue;
      }
      if (lk > rk) {
        right.advance();
        continue;
      }
      long key = lk;
      try (SpillableGroup lg = new SpillableGroup(leftBudget, groupDir, "lg");
          SpillableGroup rg = new SpillableGroup(rightBudget, groupDir, "rg")) {
        while (left.hasNext() && left.peek().key() == key) {
          CancelSignal.check(signal);
          lg.add(left.peek());
          left.advance();
        }
        while (right.hasNext() && right.peek().key() == key) {
          CancelSignal.check(signal);
          rg.add(right.peek());
          right.advance();
        }
        emit(lg, rg, leftBudget, sink, signal);
        matchedGroups++;
        outputRows += lg.count() * rg.count();
        groupSpillBytes += lg.spilledBytes() + rg.spilledBytes();
        spilledGroups += (lg.isSpilled() ? 1 : 0) + (rg.isSpilled() ? 1 : 0);
      }
    }
    return new PhaseStats(matchedGroups, spilledGroups, groupSpillBytes, outputRows);
  }

  /** Cartesian product of the two groups; block nested-loop when either side spilled. */
  private static void emit(
      SpillableGroup lg, SpillableGroup rg, long blockBudget, RowSink sink, CancelSignal signal)
      throws IOException {
    List<Row> block = new ArrayList<>();
    long blockBytes = 0;
    try (SpillableGroup.GroupCursor lc = lg.open()) {
      while (lc.hasNext()) {
        CancelSignal.check(signal);
        Row l = lc.next();
        block.add(l);
        blockBytes += l.weight();
        if (blockBytes >= blockBudget) {
          scanRight(block, rg, sink, signal);
          block.clear();
          blockBytes = 0;
        }
      }
    }
    if (!block.isEmpty()) {
      scanRight(block, rg, sink, signal);
    }
  }

  private static void scanRight(List<Row> block, SpillableGroup rg, RowSink sink,
      CancelSignal signal) throws IOException {
    try (SpillableGroup.GroupCursor rc = rg.open()) {
      while (rc.hasNext()) {
        CancelSignal.check(signal);
        Row r = rc.next();
        for (Row l : block) {
          sink.emit(l, r);
        }
      }
    }
  }
}
