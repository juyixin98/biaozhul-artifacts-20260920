package com.example.smj;

import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;

/**
 * Buffer for one key group during the merge join. Rows accumulate in memory up to the
 * group's budget share; beyond that the whole group is spilled to a temp file, so a
 * single hot key group larger than the memory budget is still joinable. The group can
 * be iterated multiple times (block nested-loop join re-scans the right side).
 */
public final class SpillableGroup implements AutoCloseable {

  /** Re-readable view over the group's rows. */
  public interface GroupCursor extends AutoCloseable {
    boolean hasNext();

    Row next() throws IOException;

    @Override
    void close();
  }

  private final long budgetBytes;
  private final Path dir;
  private final String prefix;

  private List<Row> mem = new ArrayList<>();
  private long memBytes;
  private Path spillFile;
  private BufferedWriter writer;
  private long spilledBytes;
  private long count;
  private boolean opened;

  public SpillableGroup(long budgetBytes, Path dir, String prefix) {
    this.budgetBytes = budgetBytes;
    this.dir = dir;
    this.prefix = prefix;
  }

  public void add(Row r) throws IOException {
    if (opened) {
      throw new IllegalStateException("group already opened for reading");
    }
    count++;
    if (writer != null) {
      write(r);
      return;
    }
    if (!mem.isEmpty() && memBytes + r.weight() > budgetBytes) {
      spillFile = Files.createTempFile(dir, prefix, ".grp");
      writer = Files.newBufferedWriter(spillFile, StandardCharsets.UTF_8);
      for (Row m : mem) {
        write(m);
      }
      mem = null; // release the in-memory buffer
      write(r);
      return;
    }
    mem.add(r);
    memBytes += r.weight();
  }

  private void write(Row r) throws IOException {
    SpillFile.writeRow(writer, r);
    spilledBytes += r.weight();
  }

  public boolean isSpilled() {
    return spillFile != null;
  }

  public long spilledBytes() {
    return spilledBytes;
  }

  public long count() {
    return count;
  }

  /** Open a cursor over the group. May be called repeatedly; each call re-reads. */
  public GroupCursor open() throws IOException {
    opened = true;
    if (writer != null) {
      writer.close();
      writer = null;
    }
    if (spillFile != null) {
      return new FileGroupCursor(spillFile);
    }
    return new MemGroupCursor(mem);
  }

  @Override
  public void close() {
    if (writer != null) {
      try {
        writer.close();
      } catch (IOException ignored) {
        // best effort
      }
      writer = null;
    }
    if (spillFile != null) {
      ExternalSorter.deleteQuietly(spillFile);
      spillFile = null;
    }
  }

  private static final class MemGroupCursor implements GroupCursor {
    private final Iterator<Row> it;

    MemGroupCursor(List<Row> rows) {
      this.it = rows.iterator();
    }

    @Override
    public boolean hasNext() {
      return it.hasNext();
    }

    @Override
    public Row next() {
      return it.next();
    }

    @Override
    public void close() {}
  }

  private static final class FileGroupCursor implements GroupCursor {
    private final BufferedReader reader;
    private Row current;

    FileGroupCursor(Path file) throws IOException {
      this.reader = Files.newBufferedReader(file, StandardCharsets.UTF_8);
      this.current = SpillFile.readRow(reader);
    }

    @Override
    public boolean hasNext() {
      return current != null;
    }

    @Override
    public Row next() throws IOException {
      Row r = current;
      current = SpillFile.readRow(reader);
      return r;
    }

    @Override
    public void close() {
      try {
        reader.close();
      } catch (IOException ignored) {
        // best effort
      }
    }
  }
}
