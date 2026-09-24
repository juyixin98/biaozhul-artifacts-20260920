package com.example.smj;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

/**
 * Result of sorting one input table: either an in-memory sorted list (no spill was
 * needed) or a set of sorted run files to be k-way merged on read.
 */
public record SortResult(
    List<Path> runFiles,
    List<Row> memoryRows,
    long rowCount,
    long nullKeysDropped,
    long spillWriteBytes) {

  public int runCount() {
    return runFiles.size();
  }

  public boolean spilled() {
    return !runFiles.isEmpty();
  }

  public SortedCursor openCursor(CancelSignal signal) throws IOException {
    if (runFiles.isEmpty()) {
      return new ExternalSorter.MemoryCursor(memoryRows);
    }
    return new ExternalSorter.MergeCursor(runFiles, signal);
  }

  /** Delete run files; call after any cursor opened from this result has been closed. */
  public void deleteRuns() {
    for (Path p : runFiles) {
      ExternalSorter.deleteQuietly(p);
    }
  }
}
