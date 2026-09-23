package join;

import java.io.BufferedOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * Sorted-merge equi-join over two {@link ExternalSorter.SortedSource}s.
 *
 * Equal-key groups are consumed together and joined as Cartesian products:
 *
 *   - both groups fit in the memory budget: nested loop fully in memory;
 *   - one group fits: it is materialised, the other streamed from its spill
 *     file (single pass each);
 *   - NEITHER group fits (the "single hot key group larger than RAM" case):
 *     both groups are spilled to per-group files and joined with a block
 *     nested-loops pass — blocks of one group (≤ budget) are loaded while the
 *     other group is scanned repeatedly. Bounded memory, exact multiset output.
 *
 * NULL keys never reach this class: the sorter counts and drops them.
 */
public final class JoinEngine {

    /** Output row: {"left":<original left row>,"right":<original right row>}. */
    private static final byte[] PREFIX = "{\"left\":".getBytes(java.nio.charset.StandardCharsets.UTF_8);
    private static final byte[] MIDDLE = ",\"right\":".getBytes(java.nio.charset.StandardCharsets.UTF_8);
    private static final byte[] SUFFIX = "}\n".getBytes(java.nio.charset.StandardCharsets.UTF_8);

    /** Sink for result JSONL lines. */
    public interface RowSink {
        void write(byte[] line) throws IOException;
    }

    private final long memoryBudgetBytes;
    private final Path tmpDir;
    private final String jobPrefix;

    public JoinEngine(long memoryBudgetBytes, Path tmpDir, String jobPrefix) {
        if (memoryBudgetBytes < 1) {
            throw new IllegalArgumentException("memoryBudgetBytes must be >= 1");
        }
        this.memoryBudgetBytes = memoryBudgetBytes;
        this.tmpDir = tmpDir;
        this.jobPrefix = jobPrefix;
    }

    public Stats join(ExternalSorter.SortedSource left, ExternalSorter.SortedSource right,
                      RowSink sink, TaskContext ctx)
            throws IOException, JoinCancelledException {
        Stats stats = new Stats();
        long checks = 0;
        try {
            while (true) {
                Record l = left.peek();
                Record r = right.peek();
                if (l == null || r == null) {
                    break;
                }
                int cmp = Long.compare(l.key, r.key);
                if (cmp < 0) {
                    left.advance(); // key only on the left
                } else if (cmp > 0) {
                    right.advance(); // key only on the right
                } else {
                    long k = l.key;
                    Group a = Group.collect(left, k, memoryBudgetBytes, tmpDir,
                            jobPrefix + "-ga", stats, ctx);
                    Group b;
                    try {
                        b = Group.collect(right, k, memoryBudgetBytes, tmpDir,
                                jobPrefix + "-gb", stats, ctx);
                        try {
                            joinGroup(a, b, sink, stats, ctx);
                        } finally {
                            b.close();
                        }
                    } finally {
                        a.close();
                    }
                }
                if ((++checks & 4095) == 0) {
                    ctx.checkCancelled();
                }
            }
        } finally {
            // per-group temp files are already closed/deleted via Group.close()
        }
        return stats;
    }

    private void joinGroup(Group a, Group b, RowSink sink, Stats stats, TaskContext ctx)
            throws IOException, JoinCancelledException {
        if (!a.spilled && !b.spilled) {
            for (Record x : a.records) {
                for (Record y : b.records) {
                    emit(sink, x, y);
                    stats.outputRows++;
                    if ((stats.outputRows & 8191) == 0) {
                        ctx.checkCancelled();
                    }
                }
            }
        } else if (!a.spilled) {
            scanNested(b, a.records, false, sink, stats, ctx);
        } else if (!b.spilled) {
            scanNested(a, b.records, true, sink, stats, ctx);
        } else {
            blockNestedLoops(a, b, sink, stats, ctx);
        }
    }

    /**
     * Stream every record of the spilled {@code fileGroup}, pairing it with every
     * in-memory record of {@code memRecords}.
     *
     * @param memIsLeft when true, memory records are the LEFT side of the output
     */
    private void scanNested(Group fileGroup, List<Record> memRecords, boolean memIsLeft,
                            RowSink sink, Stats stats, TaskContext ctx)
            throws IOException, JoinCancelledException {
        try (RunFiles.Reader rd = RunFiles.reader(fileGroup.file)) {
            while (!rd.atEof()) {
                Record x = rd.next();
                for (Record m : memRecords) {
                    if (memIsLeft) {
                        emit(sink, m, x);
                    } else {
                        emit(sink, x, m);
                    }
                    stats.outputRows++;
                    if ((stats.outputRows & 8191) == 0) {
                        ctx.checkCancelled();
                    }
                }
            }
        }
    }

    /**
     * Both groups are on disk and individually larger than the memory budget.
     * Load blocks of group A (up to the budget), scanning the whole B file for
     * each block. Memory usage is bounded by one block + one streaming reader.
     */
    private void blockNestedLoops(Group a, Group b, RowSink sink, Stats stats, TaskContext ctx)
            throws IOException, JoinCancelledException {
        List<Record> block = new ArrayList<>();
        long blockBytes = 0;
        try (RunFiles.Reader outer = RunFiles.reader(a.file)) {
            while (true) {
                Record x;
                if (outer.atEof()) {
                    x = null;
                } else {
                    x = outer.peek();
                }
                boolean blockFull = x == null
                        || blockBytes + x.raw.length + ExternalSorter.RECORD_OVERHEAD > memoryBudgetBytes;
                if (blockFull && !block.isEmpty()) {
                    try (RunFiles.Reader inner = RunFiles.reader(b.file)) {
                        while (!inner.atEof()) {
                            Record y = inner.next();
                            for (Record bl : block) {
                                emit(sink, bl, y);
                                stats.outputRows++;
                                if ((stats.outputRows & 8191) == 0) {
                                    ctx.checkCancelled();
                                }
                            }
                        }
                    }
                    block.clear();
                    blockBytes = 0;
                    stats.bnlBlockCount++;
                }
                if (x == null) {
                    return;
                }
                outer.next();
                block.add(x);
                blockBytes += (long) x.raw.length + ExternalSorter.RECORD_OVERHEAD;
            }
        }
    }

    static void emit(RowSink sink, Record left, Record right) throws IOException {
        // Byte-level wrapping is safe: raw lines are validated JSON objects.
        byte[] line = new byte[PREFIX.length + left.raw.length + MIDDLE.length
                + right.raw.length + SUFFIX.length];
        int p = 0;
        System.arraycopy(PREFIX, 0, line, p, PREFIX.length);
        p += PREFIX.length;
        System.arraycopy(left.raw, 0, line, p, left.raw.length);
        p += left.raw.length;
        System.arraycopy(MIDDLE, 0, line, p, MIDDLE.length);
        p += MIDDLE.length;
        System.arraycopy(right.raw, 0, line, p, right.raw.length);
        p += right.raw.length;
        System.arraycopy(SUFFIX, 0, line, p, SUFFIX.length);
        sink.write(line);
    }

    public static RowSinkAndClose closingFileSink(Path path) throws IOException {
        final OutputStream out = new BufferedOutputStream(Files.newOutputStream(path), 1 << 16);
        return new RowSinkAndClose() {
            @Override
            public void write(byte[] line) throws IOException {
                out.write(line);
            }

            @Override
            public void close() throws IOException {
                out.close();
            }
        };
    }

    public interface RowSinkAndClose extends RowSink, AutoCloseable {
        @Override
        void close() throws IOException;
    }

    /** Join result counters. */
    public static final class Stats {
        public long outputRows;
        public long groupSpillBytes;
        public int groupSpillCount;
        public long bnlBlockCount;
    }
}
