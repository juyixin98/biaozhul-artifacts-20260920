package join;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * All records sharing one join key, pulled consecutively from a
 * {@link ExternalSorter.SortedSource}.
 *
 * Records are buffered until adding the next one would exceed the memory budget;
 * the group then spills to a run file (lazily created) and streams the rest.
 * A single record larger than the whole budget is still held in memory on its
 * own — it cannot be split — which is the documented minimum-residency case.
 */
final class Group implements AutoCloseable {

    List<Record> records = new ArrayList<>();
    long memoryBytes;
    Path file;
    boolean spilled;
    int count;

    private RunFiles.Writer writer;

    private Group() {
    }

    static Group collect(ExternalSorter.SortedSource src, long key, long budgetBytes,
                         Path tmpDir, String filePrefix, JoinEngine.Stats stats,
                         TaskContext ctx)
            throws IOException, JoinCancelledException {
        Group g = new Group();
        long checks = 0;
        Record head;
        while ((head = src.peek()) != null && head.key == key) {
            long need = (long) head.raw.length + ExternalSorter.RECORD_OVERHEAD;
            if (!g.spilled
                    && !g.records.isEmpty()
                    && g.memoryBytes + need > budgetBytes) {
                Path path = tmpDir.resolve(filePrefix + "-key" + key
                        + "-grp" + stats.groupSpillCount + ".bin");
                g.writer = RunFiles.writer(path);
                for (Record r : g.records) {
                    g.writer.write(r);
                }
                g.file = path;
                g.spilled = true;
                g.records = null; // release buffered rows for GC
                stats.groupSpillCount++;
            }
            if (g.spilled) {
                g.writer.write(head);
            } else {
                g.records.add(head);
                g.memoryBytes += need;
            }
            g.count++;
            src.advance();
            if ((++checks & 4095) == 0) {
                ctx.checkCancelled();
            }
        }
        if (g.spilled) {
            g.writer.close();
            stats.groupSpillBytes += Files.size(g.file);
            g.writer = null;
        }
        return g;
    }

    @Override
    public void close() {
        if (writer != null) {
            try {
                writer.close();
            } catch (IOException ignored) {
                // cleanup path
            }
        }
        if (file != null) {
            RunFiles.deleteQuietly(file);
            file = null;
        }
    }
}
