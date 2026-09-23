package partjoin;

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/** Sink for join output rows. Streaming collectors keep memory bounded on huge outputs. */
@FunctionalInterface
public interface RowCollector {

    void collect(Row row);

    /** Collects every output row in memory (used by tests and small inline responses). */
    final class ListCollector implements RowCollector {
        private final List<Row> rows = new ArrayList<>();

        @Override
        public void collect(Row row) {
            rows.add(row);
        }

        public List<Row> rows() {
            return rows;
        }
    }

    /** Counts rows without retaining them (used for large-output tests). */
    final class CountingCollector implements RowCollector {
        private long count;

        @Override
        public void collect(Row row) {
            count++;
        }

        public long count() {
            return count;
        }
    }

    /**
     * Streams output rows to a file as JSON Lines (one JSON array per line).
     * {@code format} may be "jsonl" (default) or "json" (one JSON array document).
     */
    final class FileCollector implements RowCollector, AutoCloseable {
        private final Path path;
        private final boolean jsonArray;
        private final BufferedWriter w;
        private long count;

        public FileCollector(Path path, String format) {
            this.path = path;
            this.jsonArray = "json".equalsIgnoreCase(format);
            try {
                if (path.getParent() != null) Files.createDirectories(path.getParent());
                this.w = Files.newBufferedWriter(path, StandardCharsets.UTF_8);
                if (jsonArray) w.write('[');
            } catch (IOException e) {
                throw new JoinException(JoinException.IO_ERROR,
                        "Cannot open output file: " + path + " (" + e.getMessage() + ")", e);
            }
        }

        @Override
        public synchronized void collect(Row row) {
            try {
                String body = Json.write(row.rawList());
                if (jsonArray) {
                    if (count > 0) w.write(',');
                } else {
                    // nothing between jsonl lines
                }
                w.write(body);
                w.write('\n');
                count++;
            } catch (IOException e) {
                throw new JoinException(JoinException.IO_ERROR,
                        "Failed writing output file: " + path + " (" + e.getMessage() + ")", e);
            }
        }

        public long count() {
            return count;
        }

        public Path path() {
            return path;
        }

        @Override
        public synchronized void close() {
            try {
                if (jsonArray) {
                    w.write(']');
                    w.write('\n');
                }
                w.close();
            } catch (IOException e) {
                throw new JoinException(JoinException.IO_ERROR,
                        "Failed closing output file: " + path, e);
            }
        }
    }
}
