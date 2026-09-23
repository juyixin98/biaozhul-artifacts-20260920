package join;

import java.io.BufferedReader;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;

/**
 * External sort of one JSONL table.
 *
 * Phase 1 ("run generation"): stream the input once, keeping up to
 * {@code memoryBudgetBytes} of estimated record bytes in memory; whenever the
 * chunk reaches the budget, sort it by key and spill a sorted run file. NULL-key
 * rows are counted and dropped (they can never appear in an equi-join result).
 *
 * Phase 2 ("merge"): the sorted runs are merged with a k-way loser-free min heap.
 * To bound open file descriptors, at most {@link #MAX_OPEN_RUNS} runs are merged
 * at once; larger run counts collapse through repeated merge passes.
 *
 * If the whole table fits in the budget, no file is created and the sorted
 * ArrayList is served directly — {@link SortResult#spilled} reports this.
 */
public final class ExternalSorter {

    /** Max run files opened simultaneously by one merge. */
    public static final int MAX_OPEN_RUNS = 64;

    /**
     * Estimated heap cost of one buffered record besides its JSON bytes:
     * Record object + Long key + byte[] header + list pointer. Deliberately
     * conservative so the memory budget is a soft upper bound on buffered data.
     */
    static final int RECORD_OVERHEAD = 64;

    private ExternalSorter() {
    }

    public static SortResult sort(Path input, String keyName, long memoryBudgetBytes,
                                  Path tmpDir, String filePrefix, TaskContext ctx)
            throws IOException, JoinCancelledException {
        if (memoryBudgetBytes < 1) {
            throw new IllegalArgumentException("memoryBudgetBytes must be >= 1");
        }
        long rows = 0;
        long nullRows = 0;
        long estimatedBytes = 0;
        long checks = 0;

        List<Record> chunk = new ArrayList<>();
        List<Path> runs = new ArrayList<>();
        long initialSpillBytes = 0;
        long totalTempBytesWritten = 0;

        try (BufferedReader reader = Files.newBufferedReader(input)) {
            String line;
            long lineNo = 0;
            while ((line = reader.readLine()) != null) {
                lineNo++;
                if (line.isEmpty()) {
                    continue; // tolerate blank lines / trailing newline
                }
                final Long key;
                try {
                    Map<String, Object> obj = Json.parseObject(line);
                    Object rawKey = obj.get(keyName);
                    if (rawKey == null) {
                        key = null;
                    } else if (rawKey instanceof Long) {
                        key = (Long) rawKey;
                    } else {
                        throw new IllegalArgumentException(
                                "join key '" + keyName + "' must be an integer or null, got "
                                        + typeName(rawKey) + " (line " + lineNo + ")");
                    }
                } catch (IllegalArgumentException e) {
                    throw new IOException("Invalid JSONL in " + input + " at line " + lineNo
                            + ": " + e.getMessage(), e);
                }
                rows++;
                if (key == null) {
                    nullRows++;
                } else {
                    byte[] raw = line.getBytes(java.nio.charset.StandardCharsets.UTF_8);
                    chunk.add(new Record(key, raw));
                    estimatedBytes += (long) raw.length + RECORD_OVERHEAD;
                    if (estimatedBytes >= memoryBudgetBytes) {
                        Path run = spillChunk(chunk, tmpDir, filePrefix, runs.size());
                        runs.add(run);
                        long size = Files.size(run);
                        initialSpillBytes += size;
                        totalTempBytesWritten += size;
                        chunk.clear();
                        estimatedBytes = 0;
                    }
                }
                if ((++checks & 4095) == 0) {
                    ctx.checkCancelled();
                }
            }
        }

        int initialRuns = runs.size();
        int mergePasses = 0;

        SortedSource source;
        if (runs.isEmpty()) {
            // Everything fit in memory (or the table is empty).
            chunk.sort(Comparator.comparing(r -> r.key));
            source = new MemorySource(chunk);
        } else {
            if (!chunk.isEmpty()) {
                Path run = spillChunk(chunk, tmpDir, filePrefix, runs.size());
                runs.add(run);
                long size = Files.size(run);
                initialSpillBytes += size;
                totalTempBytesWritten += size;
                chunk = new ArrayList<>();
            }
            // Multi-pass merge until a k-way merge can consume every run at once.
            List<Path> merged = new ArrayList<>();
            while (runs.size() > MAX_OPEN_RUNS) {
                for (int from = 0; from < runs.size(); from += MAX_OPEN_RUNS) {
                    int to = Math.min(from + MAX_OPEN_RUNS, runs.size());
                    Path out = mergeRuns(runs.subList(from, to), tmpDir,
                            filePrefix + "-p" + mergePasses + "-", merged.size(), ctx);
                    merged.add(out);
                    totalTempBytesWritten += Files.size(out);
                }
                for (Path p : runs) {
                    RunFiles.deleteQuietly(p);
                }
                runs = merged;
                merged = new ArrayList<>();
                mergePasses++;
            }
            source = KWaySource.open(runs);
        }

        return new SortResult(source, runs, rows, nullRows, spilled(initialRuns),
                initialRuns, mergePasses, initialSpillBytes, totalTempBytesWritten);
    }

    private static boolean spilled(int initialRuns) {
        return initialRuns > 0;
    }

    private static Path spillChunk(List<Record> chunk, Path tmpDir, String prefix, int index)
            throws IOException {
        chunk.sort(Comparator.comparing(r -> r.key));
        Path path = tmpDir.resolve(prefix + "-run-" + index + ".bin");
        try (RunFiles.Writer w = RunFiles.writer(path)) {
            for (Record r : chunk) {
                w.write(r);
            }
        }
        return path;
    }

    private static Path mergeRuns(List<Path> inputs, Path tmpDir, String prefix,
                                  int index, TaskContext ctx)
            throws IOException, JoinCancelledException {
        Path out = tmpDir.resolve(prefix + "merge-" + index + ".bin");
        try (SortedSource src = KWaySource.open(inputs);
             RunFiles.Writer w = RunFiles.writer(out)) {
            long checks = 0;
            while (src.peek() != null) {
                w.write(src.peek());
                src.advance();
                if ((++checks & 4095) == 0) {
                    ctx.checkCancelled();
                }
            }
        }
        return out;
    }

    private static String typeName(Object o) {
        if (o instanceof Double) {
            return "number (non-integer)";
        }
        if (o instanceof String) {
            return "string";
        }
        if (o instanceof Boolean) {
            return "boolean";
        }
        if (o instanceof List) {
            return "array";
        }
        if (o instanceof Map) {
            return "object";
        }
        return o.getClass().getSimpleName();
    }

    // ------------------------------------------------------------ sources

    /** Records exposed in non-decreasing key order. */
    public interface SortedSource extends AutoCloseable {
        Record peek();

        void advance() throws IOException;

        @Override
        void close() throws IOException;
    }

    static final class MemorySource implements SortedSource {
        private final List<Record> records;
        private int pos;

        MemorySource(List<Record> records) {
            this.records = records;
        }

        @Override
        public Record peek() {
            return pos < records.size() ? records.get(pos) : null;
        }

        @Override
        public void advance() {
            pos++;
        }

        @Override
        public void close() {
            // nothing on disk
        }
    }

    /** k-way merge over sorted run files, one open reader per run. */
    static final class KWaySource implements SortedSource {
        private static final class Node {
            Record record;
            final int run;

            Node(Record record, int run) {
                this.record = record;
                this.run = run;
            }
        }

        private final List<RunFiles.Reader> readers;
        private final PriorityQueue<Node> heap;

        private KWaySource(List<RunFiles.Reader> readers) {
            this.readers = readers;
            this.heap = new PriorityQueue<>(Comparator
                    .comparingLong((Node n) -> n.record.key)
                    .thenComparingInt(n -> n.run));
            for (int i = 0; i < readers.size(); i++) {
                RunFiles.Reader r = readers.get(i);
                if (!r.atEof()) {
                    heap.add(new Node(r.peek(), i));
                }
            }
        }

        static KWaySource open(List<Path> runs) throws IOException {
            List<RunFiles.Reader> readers = new ArrayList<>(runs.size());
            boolean ok = false;
            try {
                for (Path p : runs) {
                    readers.add(RunFiles.reader(p));
                }
                KWaySource s = new KWaySource(readers);
                ok = true;
                return s;
            } finally {
                if (!ok) {
                    IOException first = null;
                    for (RunFiles.Reader r : readers) {
                        try {
                            r.close();
                        } catch (IOException e) {
                            if (first == null) {
                                first = e;
                            }
                        }
                    }
                    if (first != null) {
                        throw first;
                    }
                }
            }
        }

        @Override
        public Record peek() {
            Node n = heap.peek();
            return n == null ? null : n.record;
        }

        @Override
        public void advance() throws IOException {
            Node n = heap.poll();
            if (n == null) {
                return;
            }
            RunFiles.Reader r = readers.get(n.run);
            r.next();
            if (!r.atEof()) {
                heap.add(new Node(r.peek(), n.run));
            }
        }

        @Override
        public void close() throws IOException {
            IOException first = null;
            for (RunFiles.Reader r : readers) {
                try {
                    r.close();
                } catch (IOException e) {
                    if (first == null) {
                        first = e;
                    }
                }
            }
            if (first != null) {
                throw first;
            }
        }
    }
}
