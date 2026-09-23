package partjoin;

import java.io.BufferedReader;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.BitSet;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * Single-node, in-memory-with-disk-spill partitioned hash join.
 *
 * Algorithm overview
 * ------------------
 *  1. If both relations fit within {@code inMemoryRows} rows, a plain
 *     in-memory hash join runs (smaller side builds the hash table for INNER;
 *     the right side always builds for LEFT).
 *  2. Otherwise both sides are partitioned on their key columns using a
 *     depth-seeded Fibonacci hash. Null-keyed rows never enter partitions:
 *     INNER drops them, LEFT emits the left row padded with nulls.
 *  3. Each partition pair is processed independently:
 *       - build side fitting in memory   -> hash join of that partition;
 *       - too large, depth not exhausted -> repartition (recursive split with
 *         a fresh seed) - uniform keys split, skew does not;
 *       - too large, depth exhausted    -> bounded block-nested-loops
 *         fallback, which never builds more than {@code inMemoryRows} rows,
 *         so a pathological all-hot-key partition can always be processed.
 *
 * Duplicate keys produce the Cartesian product naturally (build-side list per
 * key bucket; every probe row emits against every matching build row).
 *
 * Disk use is bounded by {@code diskBudgetBytes} via {@link Spiller}.
 */
public final class HashJoinEngine {

    private static final int MAX_FANOUT = 64;
    private static final int MIN_FANOUT = 4;

    /** Tuning knobs for one engine instance. */
    public static final class Config {
        public int inMemoryRows = 1024;
        public long diskBudgetBytes = Long.MAX_VALUE;
        public int maxRepartitionDepth = 2;
        public String spillDir = System.getProperty("java.io.tmpdir", "/tmp")
                + "/partjoin-spill";

        public Config copy() {
            Config c = new Config();
            c.inMemoryRows = inMemoryRows;
            c.diskBudgetBytes = diskBudgetBytes;
            c.maxRepartitionDepth = maxRepartitionDepth;
            c.spillDir = spillDir;
            return c;
        }
    }

    /** Everything a run returns besides the streamed output rows. */
    public static final class EngineOut {
        public final Map<String, Object> plan;
        public final Map<String, Object> stats;

        EngineOut(Map<String, Object> plan, Map<String, Object> stats) {
            this.plan = plan;
            this.stats = stats;
        }
    }

    // ---- request-level inputs ----
    private final Table left;
    private final Table right;
    private final JoinType type;
    private final List<String> leftKeyNames;
    private final List<String> rightKeyNames;
    private final Config cfg;

    // ---- derived ----
    private final List<Integer> leftKeyIdx;
    private final List<Integer> rightKeyIdx;

    // ---- run state ----
    private RowCollector collector;
    private Spiller spiller;
    private int spillDepthReached;
    private int repartitionCount;
    private int fallbackCount;
    private int partitionsCreated;
    private int partitionsProcessed;
    private long outputRows;
    private long nullKeyLeftRows;
    private long nullKeyRightRows;
    private String mode;
    private int topFanout;

    public HashJoinEngine(Table left, Table right, JoinType type,
                          List<String> leftKeyColumns, List<String> rightKeyColumns,
                          Config config) {
        this.left = left;
        this.right = right;
        this.type = type;
        this.leftKeyNames = List.copyOf(leftKeyColumns);
        this.rightKeyNames = List.copyOf(rightKeyColumns);
        this.cfg = config == null ? new Config() : config;
        if (leftKeyColumns.isEmpty()) {
            throw new JoinException(JoinException.INVALID_REQUEST, "At least one join key column is required");
        }
        if (leftKeyColumns.size() != rightKeyColumns.size()) {
            throw new JoinException(JoinException.INVALID_REQUEST,
                    "Left/right key column counts must match (" + leftKeyColumns.size()
                            + " vs " + rightKeyColumns.size() + ")");
        }
        if (cfg.inMemoryRows < 1) {
            throw new JoinException(JoinException.INVALID_REQUEST, "inMemoryRows must be >= 1");
        }
        if (cfg.maxRepartitionDepth < 0) {
            throw new JoinException(JoinException.INVALID_REQUEST, "maxRepartitionDepth must be >= 0");
        }
        this.leftKeyIdx = left.schema().indicesOf(leftKeyColumns);
        this.rightKeyIdx = right.schema().indicesOf(rightKeyColumns);
    }

    // ------------------------------------------------------------------
    // Public API
    // ------------------------------------------------------------------

    public EngineOut execute(RowCollector collector) {
        this.collector = collector;

        if (right.rowCount() == 0) {
            mode = "EMPTY_RIGHT";
            if (type == JoinType.LEFT) {
                for (Row lr : left.rows()) {
                    if (Key.of(lr, leftKeyIdx).hasNull()) nullKeyLeftRows++;
                    emitUnmatchedLeft(lr);
                }
            }
            return finish();
        }
        if (left.rowCount() == 0) {
            mode = "EMPTY_LEFT";
            return finish();
        }

        boolean fits = left.rowCount() <= cfg.inMemoryRows && right.rowCount() <= cfg.inMemoryRows;
        if (fits) {
            mode = "IN_MEMORY";
            runInMemory();
            return finish();
        }

        mode = "PARTITIONED_SPILL";
        topFanout = pickFanout(left.rowCount(), right.rowCount());
        Path spillRoot = Path.of(cfg.spillDir, "spill-" + UUID.randomUUID());
        spiller = new Spiller(spillRoot, cfg.diskBudgetBytes);
        BitSet matched = type == JoinType.LEFT ? new BitSet(left.rowCount()) : null;
        Spiller.SpillFile[] lw = new Spiller.SpillFile[topFanout];
        Spiller.SpillFile[] rw = new Spiller.SpillFile[topFanout];
        long[] lc = new long[topFanout];
        long[] rc = new long[topFanout];
        try {
            for (int p = 0; p < topFanout; p++) {
                lw[p] = spiller.create("L0-" + p + ".jsonl");
                rw[p] = spiller.create("R0-" + p + ".jsonl");
            }
            partitionsCreated += topFanout;
            for (int i = 0; i < left.rowCount(); i++) {
                Row lr = left.rows().get(i);
                Key k = Key.of(lr, leftKeyIdx);
                if (k.hasNull()) {
                    nullKeyLeftRows++;
                    continue;
                }
                int p = partition(k, 0, topFanout);
                lw[p].write(lr, i); // probe id = original left row index
                lc[p]++;
            }
            for (Row rr : right.rows()) {
                Key k = Key.of(rr, rightKeyIdx);
                if (k.hasNull()) {
                    nullKeyRightRows++;
                    continue;
                }
                int p = partition(k, 0, topFanout);
                rw[p].write(rr, -1);
                rc[p]++;
            }
            for (int p = 0; p < topFanout; p++) {
                lw[p].close();
                rw[p].close();
            }

            for (int p = 0; p < topFanout; p++) {
                try {
                    processPartition(lw[p].path(), rw[p].path(), lc[p], rc[p], 0, matched);
                } finally {
                    lw[p].delete();
                    rw[p].delete();
                }
                partitionsProcessed++;
            }

            if (type == JoinType.LEFT) {
                for (int i = 0; i < left.rowCount(); i++) {
                    if (!matched.get(i)) emitUnmatchedLeft(left.rows().get(i));
                }
            }
        } finally {
            for (Spiller.SpillFile f : lw) if (f != null) f.close();
            for (Spiller.SpillFile f : rw) if (f != null) f.close();
            spiller.cleanup();
        }
        return finish();
    }

    /** Describes what execute() would do without touching data or disk. */
    public Map<String, Object> explain() {
        Map<String, Object> plan = basePlan();
        if (right.rowCount() == 0) {
            plan.put("mode", "EMPTY_RIGHT");
        } else if (left.rowCount() == 0) {
            plan.put("mode", "EMPTY_LEFT");
        } else if (left.rowCount() <= cfg.inMemoryRows && right.rowCount() <= cfg.inMemoryRows) {
            plan.put("mode", "IN_MEMORY");
        } else {
            plan.put("mode", "PARTITIONED_SPILL");
            topFanout = pickFanout(left.rowCount(), right.rowCount());
        }
        plan.put("fanout", "PARTITIONED_SPILL".equals(plan.get("mode")) ? topFanout : 0);
        return plan;
    }

    private Map<String, Object> basePlan() {
        Map<String, Object> plan = new LinkedHashMap<>();
        plan.put("joinType", type.name());
        plan.put("leftTable", left.name());
        plan.put("rightTable", right.name());
        plan.put("leftKeys", leftKeyNames);
        plan.put("rightKeys", rightKeyNames);
        plan.put("nullSemantics", "NULLs never match; INNER drops null-keyed rows, "
                + "LEFT pads non-matching left rows with NULLs");
        plan.put("duplicateSemantics", "matching rows form a Cartesian product");
        plan.put("inMemoryRows", cfg.inMemoryRows);
        plan.put("diskBudgetBytes",
                cfg.diskBudgetBytes == Long.MAX_VALUE ? "unlimited" : cfg.diskBudgetBytes);
        plan.put("maxRepartitionDepth", cfg.maxRepartitionDepth);
        plan.put("spillDir", cfg.spillDir);
        plan.put("buildSide", type == JoinType.LEFT
                ? "right (inner table)"
                : (right.rowCount() <= left.rowCount() ? "right (smaller)" : "left (smaller)"));
        plan.put("fallback", "block-nested-loops, chunk size " + cfg.inMemoryRows
                + " rows; used when repartition cannot shrink a partition");
        return plan;
    }

    // ------------------------------------------------------------------
    // In-memory join
    // ------------------------------------------------------------------

    private void runInMemory() {
        boolean buildRight = type == JoinType.LEFT || right.rowCount() <= left.rowCount();
        if (buildRight) {
            Map<Key, List<Row>> ht = buildHashMap(right.rows(), rightKeyIdx, true);
            BitSet matched = type == JoinType.LEFT ? new BitSet(left.rowCount()) : null;
            for (int id = 0; id < left.rowCount(); id++) {
                Row lr = left.rows().get(id);
                Key k = Key.of(lr, leftKeyIdx);
                if (k.hasNull()) {
                    nullKeyLeftRows++;
                    continue;
                }
                List<Row> hits = ht.get(k);
                if (hits != null) {
                    if (matched != null) matched.set(id);
                    for (Row rr : hits) emitMatch(lr, rr);
                }
            }
            if (type == JoinType.LEFT) {
                for (int i = 0; i < left.rowCount(); i++) {
                    if (!matched.get(i)) emitUnmatchedLeft(left.rows().get(i));
                }
            }
        } else {
            // INNER only: smaller left side builds; null rows on either side are dropped.
            Map<Key, List<Row>> ht = buildHashMap(left.rows(), leftKeyIdx, false);
            for (Row rr : right.rows()) {
                Key k = Key.of(rr, rightKeyIdx);
                if (k.hasNull()) {
                    nullKeyRightRows++;
                    continue;
                }
                List<Row> hits = ht.get(k);
                if (hits != null) for (Row lr : hits) emitMatch(lr, rr);
            }
        }
    }

    private Map<Key, List<Row>> buildHashMap(List<Row> rows, List<Integer> keyIdx, boolean rightSide) {
        Map<Key, List<Row>> ht = new HashMap<>();
        for (Row r : rows) {
            Key k = Key.of(r, keyIdx);
            if (k.hasNull()) {
                if (rightSide) nullKeyRightRows++;
                else nullKeyLeftRows++;
                continue;
            }
            ht.computeIfAbsent(k, x -> new ArrayList<>()).add(r);
        }
        return ht;
    }

    // ------------------------------------------------------------------
    // Partitioned spill join
    // ------------------------------------------------------------------

    private void processPartition(Path lPath, Path rPath, long lCount, long rCount,
                                  int depth, BitSet matched) {
        // Empty side -> no matches. LEFT unmatched rows are emitted by the
        // top-level final pass over the original left table.
        if (rCount == 0 || lCount == 0) return;

        // For INNER, the smaller side of this partition may build even when
        // the global "right is build" assumption does not hold. LEFT always
        // builds right, because unmatched left rows must be detectable via the
        // probe id bitmap.
        boolean buildRight = type == JoinType.LEFT || rCount <= lCount;
        long buildCount = buildRight ? rCount : lCount;
        List<Integer> buildKeyIdx = buildRight ? rightKeyIdx : leftKeyIdx;
        List<Integer> probeKeyIdx = buildRight ? leftKeyIdx : rightKeyIdx;
        Path buildPath = buildRight ? rPath : lPath;
        Path probePath = buildRight ? lPath : rPath;

        if (buildCount <= cfg.inMemoryRows) {
            hashJoinPartition(buildPath, probePath, buildKeyIdx, probeKeyIdx,
                    buildRight, matched);
            return;
        }

        // Build side too large: split further or fall back.
        if (depth >= cfg.maxRepartitionDepth) {
            fallbackJoin(buildPath, probePath, buildKeyIdx, probeKeyIdx,
                    buildRight, matched);
            return;
        }

        int newDepth = depth + 1;
        spillDepthReached = Math.max(spillDepthReached, newDepth);
        repartitionCount++;
        int fanout = pickFanout(lCount, rCount);
        String tag = "D" + newDepth + "-" + Long.toHexString(UUID.randomUUID().getMostSignificantBits());
        Spiller.SpillFile[] lw = new Spiller.SpillFile[fanout];
        Spiller.SpillFile[] rw = new Spiller.SpillFile[fanout];
        long[] nc = new long[fanout];
        long[] nrc = new long[fanout];
        for (int p = 0; p < fanout; p++) {
            lw[p] = spiller.create(tag + "-L" + p + ".jsonl");
            rw[p] = spiller.create(tag + "-R" + p + ".jsonl");
        }
        partitionsCreated += fanout;
        for (Spiller.Envelope e : Spiller.readAll(lPath)) {
            int p = partition(Key.of(e.row(), leftKeyIdx), newDepth, fanout);
            lw[p].write(e.row(), e.id());
            nc[p]++;
        }
        for (Spiller.Envelope e : Spiller.readAll(rPath)) {
            int p = partition(Key.of(e.row(), rightKeyIdx), newDepth, fanout);
            rw[p].write(e.row(), e.id());
            nrc[p]++;
        }
        for (int p = 0; p < fanout; p++) {
            lw[p].close();
            rw[p].close();
        }
        for (int p = 0; p < fanout; p++) {
            try {
                processPartition(lw[p].path(), rw[p].path(), nc[p], nrc[p], newDepth, matched);
            } finally {
                lw[p].delete();
                rw[p].delete();
            }
            partitionsProcessed++;
        }
    }

    /** Hash join of one partition pair whose build side fits in memory. */
    private void hashJoinPartition(Path buildPath, Path probePath,
                                   List<Integer> buildKeyIdx, List<Integer> probeKeyIdx,
                                   boolean buildIsRight, BitSet matched) {
        List<Spiller.Envelope> buildEnv = Spiller.readAll(buildPath);
        Map<Key, List<Spiller.Envelope>> ht = new HashMap<>();
        for (Spiller.Envelope e : buildEnv) {
            ht.computeIfAbsent(Key.of(e.row(), buildKeyIdx), k -> new ArrayList<>()).add(e);
        }
        try (BufferedReader br = Files.newBufferedReader(probePath, StandardCharsets.UTF_8)) {
            String line;
            while ((line = br.readLine()) != null) {
                if (line.isEmpty()) continue;
                Map<String, Object> env = Json.parseObject(line);
                Row probe = Row.ofRaw(Json.getArray(env, "row"));
                int id = ((Number) env.get("id")).intValue();
                List<Spiller.Envelope> hits = ht.get(Key.of(probe, probeKeyIdx));
                if (hits == null) continue;
                for (Spiller.Envelope be : hits) {
                    Row l = buildIsRight ? probe : be.row();
                    Row r = buildIsRight ? be.row() : probe;
                    if (buildIsRight && matched != null) matched.set(id);
                    emitMatch(l, r);
                }
            }
        } catch (IOException e) {
            throw new JoinException(JoinException.IO_ERROR,
                    "Failed reading spill file: " + probePath, e);
        }
    }

    /**
     * Bounded block-nested-loops fallback for hot partitions repartitioning
     * cannot shrink. Both sides are streamed from disk in chunks of at most
     * {@code inMemoryRows} rows; peak retained memory is two chunks plus a
     * hash table over one chunk. A pathological all-hot-key partition is
     * therefore always processable regardless of cardinality.
     */
    private void fallbackJoin(Path buildPath, Path probePath,
                              List<Integer> buildKeyIdx, List<Integer> probeKeyIdx,
                              boolean buildIsRight, BitSet matched) {
        fallbackCount++;
        int chunk = cfg.inMemoryRows;
        try (BufferedReader pr = Files.newBufferedReader(probePath, StandardCharsets.UTF_8)) {
            List<Spiller.Envelope> probeChunk = new ArrayList<>(chunk);
            String pline;
            boolean probeDone = false;
            while (!probeDone) {
                probeChunk.clear();
                while (probeChunk.size() < chunk && (pline = pr.readLine()) != null) {
                    if (pline.isEmpty()) continue;
                    probeChunk.add(parseEnvelope(pline));
                }
                if (probeChunk.isEmpty()) break;
                probeDone = probeChunk.size() < chunk;

                try (BufferedReader br = Files.newBufferedReader(buildPath, StandardCharsets.UTF_8)) {
                    List<Spiller.Envelope> buildChunk = new ArrayList<>(chunk);
                    String bline;
                    while (true) {
                        buildChunk.clear();
                        while (buildChunk.size() < chunk && (bline = br.readLine()) != null) {
                            if (bline.isEmpty()) continue;
                            buildChunk.add(parseEnvelope(bline));
                        }
                        if (buildChunk.isEmpty()) break;
                        joinChunks(buildChunk, probeChunk, buildKeyIdx, probeKeyIdx,
                                buildIsRight, matched);
                    }
                }
            }
        } catch (IOException e) {
            throw new JoinException(JoinException.IO_ERROR,
                    "Block-nested-loops fallback failed (" + buildPath + ", " + probePath + ")", e);
        }
    }

    private static Spiller.Envelope parseEnvelope(String line) {
        Map<String, Object> env = Json.parseObject(line);
        return new Spiller.Envelope(
                Row.ofRaw(Json.getArray(env, "row")),
                ((Number) env.get("id")).intValue());
    }

    private void joinChunks(List<Spiller.Envelope> buildChunk,
                            List<Spiller.Envelope> probeChunk,
                            List<Integer> buildKeyIdx, List<Integer> probeKeyIdx,
                            boolean buildIsRight, BitSet matched) {
        Map<Key, List<Spiller.Envelope>> ht = new HashMap<>();
        for (Spiller.Envelope e : buildChunk) {
            ht.computeIfAbsent(Key.of(e.row(), buildKeyIdx), k -> new ArrayList<>()).add(e);
        }
        for (Spiller.Envelope pe : probeChunk) {
            List<Spiller.Envelope> hits = ht.get(Key.of(pe.row(), probeKeyIdx));
            if (hits == null) continue;
            for (Spiller.Envelope be : hits) {
                Row l = buildIsRight ? pe.row() : be.row();
                Row r = buildIsRight ? be.row() : pe.row();
                if (buildIsRight && matched != null) matched.set(pe.id());
                emitMatch(l, r);
            }
        }
    }

    // ------------------------------------------------------------------
    // Output and stats
    // ------------------------------------------------------------------

    private void emitMatch(Row l, Row r) {
        Object[] out = new Object[l.size() + r.size()];
        for (int i = 0; i < l.size(); i++) out[i] = l.get(i).raw();
        for (int i = 0; i < r.size(); i++) out[l.size() + i] = r.get(i).raw();
        collector.collect(Row.of(out));
        outputRows++;
    }

    private void emitUnmatchedLeft(Row l) {
        Object[] out = new Object[l.size() + right.schema().size()];
        for (int i = 0; i < l.size(); i++) out[i] = l.get(i).raw();
        // trailing entries stay null
        collector.collect(Row.of(out));
        outputRows++;
    }

    private EngineOut finish() {
        Map<String, Object> stats = new LinkedHashMap<>();
        stats.put("mode", mode);
        stats.put("leftInputRows", left.rowCount());
        stats.put("rightInputRows", right.rowCount());
        stats.put("outputRows", outputRows);
        stats.put("nullKeyLeftRows", nullKeyLeftRows);
        stats.put("nullKeyRightRows", nullKeyRightRows);
        stats.put("partitionsCreated", partitionsCreated);
        stats.put("partitionsProcessed", partitionsProcessed);
        stats.put("maxSpillDepthReached", spillDepthReached);
        stats.put("repartitions", repartitionCount);
        stats.put("fallbackBlockNestedLoops", fallbackCount);
        if (spiller != null) {
            stats.put("spillFilesCreated", spiller.fileCount());
            stats.put("spillBytesWritten", spiller.totalBytesWritten());
            stats.put("spillDirectory", spiller.directory().toString());
        } else {
            stats.put("spillFilesCreated", 0);
            stats.put("spillBytesWritten", 0);
        }

        Map<String, Object> plan = basePlan();
        plan.put("mode", mode);
        plan.put("fanout", topFanout);
        return new EngineOut(plan, stats);
    }

    // ------------------------------------------------------------------
    // Hashing / partitioning
    // ------------------------------------------------------------------

    private int pickFanout(long lRows, long rRows) {
        long bigger = Math.max(lRows, rRows);
        int f = (int) Math.ceil(bigger * 1.3 / (double) cfg.inMemoryRows);
        return Math.max(MIN_FANOUT, Math.min(MAX_FANOUT, f));
    }

    /**
     * Depth-seeded hash of a composite key. The seed changes per repartition
     * depth so a bad collision at depth d does not repeat at depth d+1.
     */
    private int partition(Key k, int depth, int fanout) {
        long h = mix((long) k.hashCode() + depthSeed(depth));
        return Math.floorMod(h, fanout);
    }

    private static long depthSeed(int depth) {
        long s = 0x9E3779B97F4A7C15L;
        for (int i = 0; i <= depth; i++) {
            s = mix(s + (long) (i + 1) * 0xC2B2AE3D27D4EB4FL);
        }
        return s;
    }

    private static long mix(long x) {
        x ^= x >>> 33;
        x *= 0xff51afd7ed558ccdL;
        x ^= x >>> 33;
        x *= 0xc4ceb9fe1a85ec53L;
        x ^= x >>> 33;
        return x;
    }
}
