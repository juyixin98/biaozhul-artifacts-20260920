package partjoin;

import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Owns the spill directory and enforces a cumulative-bytes disk budget.
 *
 * Every byte written to a spill file is counted against {@code diskBudgetBytes}.
 * The budget is checked BEFORE each line is written, so a request exceeding it
 * fails fast with {@link JoinException#DISK_BUDGET_EXHAUSTED}; all files and the
 * temporary directory are then removed by {@link #cleanup()}.
 *
 * Deleting a file (or closing the spiller) releases its bytes, which models
 * deletion freeing space while still rejecting the very first write that would
 * take live spill volume above the budget.
 */
public final class Spiller implements AutoCloseable {

    private final Path dir;
    private final long budgetBytes;
    private long bytesUsed;
    private long totalBytesWritten;
    private int fileCount;
    private boolean dirCreated;
    private final List<SpillFile> openFiles = new ArrayList<>();

    public Spiller(Path dir, long budgetBytes) {
        this.dir = dir;
        this.budgetBytes = budgetBytes;
    }

    public Path directory() {
        return dir;
    }

    public long bytesUsed() {
        return bytesUsed;
    }

    public long totalBytesWritten() {
        return totalBytesWritten;
    }

    public int fileCount() {
        return fileCount;
    }

    private void ensureDir() {
        if (!dirCreated) {
            try {
                Files.createDirectories(dir);
            } catch (IOException e) {
                throw new JoinException(JoinException.IO_ERROR,
                        "Cannot create spill directory: " + dir + " (" + e.getMessage() + ")", e);
            }
            dirCreated = true;
        }
    }

    /** Opens a new spill file with the given logical name (relative to the spill dir). */
    public SpillFile create(String name) {
        ensureDir();
        fileCount++;
        Path p = dir.resolve(name);
        try {
            BufferedWriter w = Files.newBufferedWriter(p, StandardCharsets.UTF_8);
            SpillFile f = new SpillFile(p, w);
            synchronized (this) {
                openFiles.add(f);
            }
            return f;
        } catch (IOException e) {
            throw new JoinException(JoinException.IO_ERROR,
                    "Cannot create spill file: " + p + " (" + e.getMessage() + ")", e);
        }
    }

    /** Streams JSON-envelope lines ({row:[...], id:n}) back from a spill file. */
    public static List<Envelope> readAll(Path p) {
        List<Envelope> out = new ArrayList<>();
        try (BufferedReader r = Files.newBufferedReader(p, StandardCharsets.UTF_8)) {
            String line;
            while ((line = r.readLine()) != null) {
                if (line.isEmpty()) continue;
                Map<String, Object> env = Json.parseObject(line);
                Row row = Row.ofRaw(Json.getArray(env, "row"));
                Object idObj = env.get("id");
                int id = idObj instanceof Number n ? n.intValue() : -1;
                out.add(new Envelope(row, id));
            }
        } catch (IOException e) {
            throw new JoinException(JoinException.IO_ERROR,
                    "Cannot read spill file: " + p + " (" + e.getMessage() + ")", e);
        }
        return out;
    }

    public synchronized void release(long bytes, Path p) {
        try {
            Files.deleteIfExists(p);
        } catch (IOException e) {
            throw new JoinException(JoinException.IO_ERROR,
                    "Cannot delete spill file: " + p + " (" + e.getMessage() + ")", e);
        }
        bytesUsed -= bytes;
    }

    public void cleanup() {
        if (!dirCreated || !Files.exists(dir)) return;
        // Close every still-open writer (e.g. mid-partition failure) so no
        // file handle leaks past a failed run, then remove the whole tree.
        synchronized (this) {
            for (SpillFile f : openFiles) f.closeQuietly();
        }
        try (var paths = Files.walk(dir)) {
            paths.sorted((a, b) -> b.getNameCount() - a.getNameCount())
                    .forEach(p -> {
                        try {
                            Files.deleteIfExists(p);
                        } catch (IOException e) {
                            throw new UncheckedIOException(e);
                        }
                    });
        } catch (IOException | UncheckedIOException e) {
            throw new JoinException(JoinException.IO_ERROR,
                    "Failed cleaning spill directory: " + dir + " (" + e.getMessage() + ")", e);
        }
        dirCreated = false;
        bytesUsed = 0;
        synchronized (this) {
            openFiles.clear();
        }
    }

    @Override
    public void close() {
        cleanup();
    }

    /** One spilled row plus the probe-side original row index (-1 on the build side). */
    public record Envelope(Row row, int id) {
    }

    /** A single spill file; each write is charged against the disk budget. */
    public final class SpillFile implements AutoCloseable {
        private final Path path;
        private final BufferedWriter writer;
        private long fileBytes;
        private boolean closed;

        SpillFile(Path path, BufferedWriter writer) {
            this.path = path;
            this.writer = writer;
        }

        public Path path() {
            return path;
        }

        public long fileBytes() {
            return fileBytes;
        }

        public void write(Row row, int id) {
            String line = Json.write(Map.of("row", row.rawList(), "id", id));
            byte[] bytes = (line + "\n").getBytes(StandardCharsets.UTF_8);
            synchronized (Spiller.this) {
                if (bytesUsed + (long) bytes.length > budgetBytes) {
                    throw new JoinException(JoinException.DISK_BUDGET_EXHAUSTED,
                            "Disk budget of " + budgetBytes + " bytes exceeded while spilling "
                                    + "(would use " + (bytesUsed + bytes.length)
                                    + " bytes) at " + path.getFileName());
                }
                bytesUsed += bytes.length;
                totalBytesWritten += bytes.length;
                fileBytes += bytes.length;
            }
            try {
                writer.write(line);
                writer.write('\n');
            } catch (IOException e) {
                throw new JoinException(JoinException.IO_ERROR,
                        "Failed writing spill file: " + path + " (" + e.getMessage() + ")", e);
            }
        }

        @Override
        public void close() {
            if (closed) return;
            closed = true;
            try {
                writer.close();
            } catch (IOException e) {
                throw new JoinException(JoinException.IO_ERROR,
                        "Failed closing spill file: " + path, e);
            } finally {
                synchronized (Spiller.this) {
                    openFiles.remove(this);
                }
            }
        }

        void closeQuietly() {
            if (closed) return;
            closed = true;
            try {
                writer.close();
            } catch (IOException ignore) {
                // best-effort during cleanup
            }
        }

        /** Removes the file and releases its bytes from the live budget. */
        public void delete() {
            close();
            Spiller.this.release(fileBytes, path);
        }
    }
}
