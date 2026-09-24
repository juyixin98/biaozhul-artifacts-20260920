package com.example.smj;

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * Orchestrates one join: externally sort both inputs, then merge-join them into the
 * output file. All temporary files live under {@code taskTempDir}; sort run files are
 * deleted here on every exit path, and the caller removes the whole temp directory.
 */
public final class JoinEngine {

  private JoinEngine() {}

  public static JoinStats run(JoinRequest req, Path taskTempDir, CancelSignal signal)
      throws IOException {
    Files.createDirectories(taskTempDir);
    JoinStats stats = new JoinStats();
    SortResult left = null;
    SortResult right = null;
    try {
      left = ExternalSorter.sort(
          Path.of(req.leftPath()), req.keyField(), req.memoryBudgetBytes(), taskTempDir, "L",
          signal);
      right = ExternalSorter.sort(
          Path.of(req.rightPath()), req.keyField(), req.memoryBudgetBytes(), taskTempDir, "R",
          signal);
      stats.leftRows = left.rowCount();
      stats.rightRows = right.rowCount();
      stats.leftNullKeysDropped = left.nullKeysDropped();
      stats.rightNullKeysDropped = right.nullKeysDropped();
      stats.leftRuns = left.runCount();
      stats.rightRuns = right.runCount();
      stats.sortSpillWriteBytes = left.spillWriteBytes() + right.spillWriteBytes();

      Path out = Path.of(req.outputPath());
      if (out.getParent() != null) {
        Files.createDirectories(out.getParent());
      }
      CountingSink sink = new CountingSink(Files.newBufferedWriter(out, StandardCharsets.UTF_8));
      MergeJoiner.PhaseStats phase;
      try (SortedCursor lc = left.openCursor(signal);
          SortedCursor rc = right.openCursor(signal);
          CountingSink s = sink) {
        phase = MergeJoiner.join(lc, rc, s, req.memoryBudgetBytes(), taskTempDir, signal);
      }
      stats.matchedKeyGroups = phase.matchedKeyGroups();
      stats.spilledKeyGroups = phase.spilledKeyGroups();
      stats.groupSpillWriteBytes = phase.groupSpillWriteBytes();
      stats.outputRows = phase.outputRows();
      return stats;
    } finally {
      if (left != null) {
        left.deleteRuns();
      }
      if (right != null) {
        right.deleteRuns();
      }
    }
  }

  /** Writes {@code {"left":<row>,"right":<row>}} lines and counts them. */
  private static final class CountingSink implements RowSink, AutoCloseable {
    private final BufferedWriter w;

    CountingSink(BufferedWriter w) {
      this.w = w;
    }

    @Override
    public void emit(Row left, Row right) throws IOException {
      w.write("{\"left\":");
      w.write(left.json());
      w.write(",\"right\":");
      w.write(right.json());
      w.write('}');
      w.newLine();
    }

    @Override
    public void close() throws IOException {
      w.close();
    }
  }
}
