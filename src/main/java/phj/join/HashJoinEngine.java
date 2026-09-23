package phj.join;

import phj.core.JoinType;
import phj.core.Key;
import phj.core.QueryRequest;
import phj.core.QueryResult;
import phj.core.Relation;
import phj.core.Row;
import phj.core.Value;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 分区哈希连接引擎（Partitioned / Grace Hash Join）。
 *
 * 执行模型：
 *  1. 选建表侧：LEFT 固定右表为建表侧（保证左行保留）；INNER 选行数较少的一侧。
 *  2. 建表侧总行数 &lt;= 内存阈值时直接走内存哈希连接（不落盘）。
 *  3. 否则按哈希把两侧切成 P 个分区落盘（JSON Lines），逐分区处理：
 *       - 建表分区行数 &lt;= 阈值：装入内存哈希表，流式探测；
 *       - 超限：先判定热点（单一键占比 / 不同键数），热点直接走有界回退；
 *               否则换种子递归再分区，直至 maxRecursiveLevels，再不行走回退；
 *  4. 有界回退 = 分块嵌套循环（Block Nested Loop）：
 *       建表侧按阈值切块、逐块扫探测侧，内存占用上限 ≈ 阈值行；
 *       LEFT 用磁盘位图标记文件记录探测行是否匹配，最后补未匹配行。
 *
 * NULL 语义：连接键任一列含 NULL 的行永不匹配；INNER 直接丢弃，
 * LEFT 保留左行并以右列全 NULL 补位。NULL 键行不参与哈希分区落盘。
 *
 * 重复键：内存哈希表按 Key 聚成链表，探测时输出全部配对（笛卡尔积）。
 */
public final class HashJoinEngine {

    private final QueryRequest req;
    private final StatsCollector stats = new StatsCollector();
    private StatsCollector.Node root;

    private SpillStore spill;
    private int[] buildKeyIdx;
    private int[] probeKeyIdx;
    private boolean buildIsRight;
    private long threshold;

    // 全局计数
    private long outputRows;
    private long spillWaves;             // 落盘的分区波次（初始 + 每次递归）
    private long inMemoryPartitions;
    private long bnlFallbacks;
    private long hotKeyFallbacks;
    private long nullBuildRows;
    private long nullProbeRows;

    public HashJoinEngine(QueryRequest req) {
        this.req = req;
    }

    /** 收集全部结果行到内存（默认入口）。 */
    public QueryResult execute() {
        List<Row> collected = new ArrayList<>();
        QueryResult qr = execute(collected::add);
        qr.rows = collected;
        return qr;
    }

    /** 流式入口：结果行逐条推给 sink，避免超大结果集撑爆内存；qr.rows 为 null。 */
    public QueryResult execute(RowSink sink) {
        return run(sink);
    }

    private QueryResult run(RowSink sink) {
        Relation left = req.left;
        Relation right = req.right;
        int[][] idx = req.resolveKeyIndices(left, right);
        int[] lKey = idx[0];
        int[] rKey = idx[1];

        root = stats.root("partitionedHashJoin");
        root.put("joinType", req.joinType.name());
        root.put("keyColumns", keyColumnNames());

        List<String> outCols = new ArrayList<>(left.columns());
        outCols.addAll(right.columns());

        Path baseDir = req.spillDir != null ? req.spillDir : Path.of(System.getProperty("java.io.tmpdir"));
        String tag = Long.toHexString(System.nanoTime()) + "-" + Thread.currentThread().threadId();
        spill = new SpillStore(baseDir, req.diskQuotaBytes, req.keepSpillFiles, tag);
        threshold = req.memoryThresholdRows;

        // 建表侧选择
        if (req.joinType == JoinType.LEFT) {
            buildIsRight = true;
        } else {
            buildIsRight = right.rowCount() <= left.rowCount();
        }
        Relation buildRel = buildIsRight ? right : left;
        Relation probeRel = buildIsRight ? left : right;
        buildKeyIdx = buildIsRight ? rKey : lKey;
        probeKeyIdx = buildIsRight ? lKey : rKey;

        root.put("buildSide", buildIsRight ? "right" : "left");
        root.put("buildRowsInput", buildRel.rowCount());
        root.put("probeRowsInput", probeRel.rowCount());
        root.put("options", req.toPlanOptions());

        try {
            // 先摘出 NULL 键行（不进入哈希表 / 不参与分区）
            List<Row> buildValid = new ArrayList<>();
            for (Row r : buildRel.rows()) {
                Key k = Key.ofRow(r, buildKeyIdx);
                if (k.isNull()) nullBuildRows++;
                else buildValid.add(r);
            }
            List<Row> probeValid = new ArrayList<>();
            List<Row> probeNull = new ArrayList<>();
            for (Row r : probeRel.rows()) {
                Key k = Key.ofRow(r, probeKeyIdx);
                if (k.isNull()) {
                    nullProbeRows++;
                    probeNull.add(r);
                } else {
                    probeValid.add(r);
                }
            }

            // LEFT 连接中，探测侧（必为左表）NULL 键行无匹配，直接补 NULL 输出
            if (req.joinType == JoinType.LEFT) {
                for (Row r : probeNull) {
                    emit(sink, r, null);
                }
            }

            if (buildValid.size() <= threshold) {
                // 快速路径：纯内存
                StatsCollector.Node n = root.child("inMemoryHashJoin");
                n.put("buildRows", buildValid.size());
                n.put("probeRows", probeValid.size());
                runInMemory(buildValid, probeValid, sink, n);
            } else {
                runPartitioned(buildValid, probeValid, sink);
            }

            // INNER 时探测侧 NULL 键行被丢弃；LEFT 已在上面补位。
            QueryResult qr = new QueryResult();
            qr.outputColumns = outCols;
            qr.rows = null; // 流式入口不收集；execute() 包装层会回填
            qr.planRoot = root;
            qr.stats = finalizeStats();
            qr.spillDir = req.keepSpillFiles ? spill.dir().toString() : null;
            return qr;
        } finally {
            spill.close();
        }
    }

    // ---------------------------------------------------------------- 内存路径

    /**
     * 内存哈希连接。buildRows 全部装入 HashMap&lt;Key,List&lt;Row&gt;&gt;。
     * LEFT 时对每个探测行记录是否匹配，未匹配补空行。
     */
    private void runInMemory(List<Row> buildRows, List<Row> probeRows,
                             RowSink sink, StatsCollector.Node node) {
        Map<Key, List<Row>> table = new HashMap<>(Math.max(16, buildRows.size() * 2));
        for (Row br : buildRows) {
            Key k = Key.ofRow(br, buildKeyIdx);
            table.computeIfAbsent(k, x -> new ArrayList<>()).add(br);
        }
        node.put("buildDistinctKeys", table.size());
        boolean leftJoin = req.joinType == JoinType.LEFT;
        long matchedPairs = 0;
        long unmatchedProbe = 0;
        for (Row pr : probeRows) {
            Key k = Key.ofRow(pr, probeKeyIdx);
            List<Row> matches = table.get(k);
            if (matches == null) {
                if (leftJoin) {
                    emit(sink, pr, null);
                    unmatchedProbe++;
                }
            } else {
                for (Row br : matches) {
                    emit(sink, pr, br);
                    matchedPairs++;
                }
            }
        }
        node.put("matchedPairs", matchedPairs);
        node.put("unmatchedProbeRows", unmatchedProbe);
        inMemoryPartitions++;
    }

    // ------------------------------------------------------------ 分区主流程

    private void runPartitioned(List<Row> buildRows, List<Row> probeRows, RowSink sink) {
        int p = effectivePartitions(buildRows.size());
        root.put("initialPartitions", p);

        List<SpillFile> buildParts = newSpillFiles(p, "L0-build-");
        List<SpillFile> probeParts = newSpillFiles(p, "L0-probe-");
        long seed = levelSeed(0);
        long[] buildCount = new long[p];
        long[] probeCount = new long[p];

        partitionWrite(buildRows, buildKeyIdx, buildParts, buildCount, seed);
        partitionWrite(probeRows, probeKeyIdx, probeParts, probeCount, seed);

        StatsCollector.Node partNode = root.child("partitionStage");
        partNode.put("level", 0);
        partNode.put("partitions", p);
        partNode.put("seed", seed);
        spillWaves++;

        long maxBuildPart = 0;
        for (int i = 0; i < p; i++) maxBuildPart = Math.max(maxBuildPart, buildCount[i]);
        partNode.put("maxBuildPartitionRows", maxBuildPart);

        for (int i = 0; i < p; i++) {
            buildParts.get(i).finishWriting();
            probeParts.get(i).finishWriting();
        }

        for (int i = 0; i < p; i++) {
            StatsCollector.Node pn = root.child("partitionProcess");
            pn.put("partitionId", i);
            pn.put("buildRows", buildCount[i]);
            pn.put("probeRows", probeCount[i]);
            processPartition(buildParts.get(i), probeParts.get(i), probeCount[i],
                    sink, 1, pn);
        }

        // 初始分区文件处理完即删（回退/递归内部会自行管理子文件）
        for (int i = 0; i < p; i++) {
            buildParts.get(i).delete();
            probeParts.get(i).delete();
        }
    }

    /** 递归处理一对同分区文件。 */
    private void processPartition(SpillFile buildFile, SpillFile probeFile, long probeRows,
                                  RowSink sink, int level, StatsCollector.Node node) {
        long bn = buildFile.rowCount();
        if (bn == 0) {
            // 空建表分区：INNER 无输出；LEFT 探测行全部补 NULL
            if (req.joinType == JoinType.LEFT) {
                long[] unmatched = {0};
                probeFile.forEachRow(pr -> {
                    emit(sink, pr, null);
                    unmatched[0]++;
                });
                node.put("unmatchedProbeRows", unmatched[0]);
            }
            node.put("strategy", "emptyBuild");
            buildFile.delete();
            probeFile.delete();
            return;
        }
        if (bn <= threshold) {
            node.put("strategy", "inMemoryHashJoin");
            inMemoryPartitions++;
            List<Row> buildRows = buildFile.readAll(); // 行数 <= threshold，可整体驻留
            Map<Key, List<Row>> table = new HashMap<>(Math.max(16, buildRows.size() * 2));
            for (Row br : buildRows) {
                table.computeIfAbsent(Key.ofRow(br, buildKeyIdx), k -> new ArrayList<>()).add(br);
            }
            node.put("buildDistinctKeys", table.size());
            node.put("spillBytesBuild", buildFile.bytes());
            node.put("spillBytesProbe", probeFile.bytes());

            boolean leftJoin = req.joinType == JoinType.LEFT;
            // LEFT 需要知道每个探测行是否匹配：探测分区小则内存位图，否则磁盘位图。
            ProbeMarker marker = leftJoin
                    ? (probeRows <= threshold ? new MemoryProbeMarker((int) probeRows)
                                              : new DiskProbeMarker(spill, probeFile.label(), probeRows))
                    : null;
            long matchedPairs = 0;
            try {
                long[] idx = {0};
                boolean useMarker = marker != null;
                long[] pairs = {0};
                probeFile.forEachRow(pr -> {
                    long i = idx[0]++;
                    Key k = Key.ofRow(pr, probeKeyIdx);
                    List<Row> matches = table.get(k);
                    if (matches != null) {
                        for (Row br : matches) {
                            emit(sink, pr, br);
                            pairs[0]++;
                        }
                        if (useMarker) marker.mark(i);
                    }
                });
                matchedPairs = pairs[0];
                node.put("matchedPairs", matchedPairs);

                if (leftJoin) {
                    long[] unmatched = {0};
                    long[] uidx = {0};
                    probeFile.forEachRow(pr -> {
                        long i = uidx[0]++;
                        if (!marker.isMarked(i)) {
                            emit(sink, pr, null);
                            unmatched[0]++;
                        }
                    });
                    node.put("unmatchedProbeRows", unmatched[0]);
                    node.put("marker", marker.kind());
                }
            } finally {
                if (marker != null) marker.close();
            }
            buildFile.delete();
            probeFile.delete();
            return;
        }

        // 超限：判定热点键
        HotKeyInfo hot = detectHotKey(buildFile);
        node.put("distinctKeysSampled", hot.distinctKeys);
        node.put("maxKeyFrequency", hot.maxFreq);
        if (hot.isHot(threshold) || level > req.maxRecursiveLevels) {
            String reason = level > req.maxRecursiveLevels
                    ? "达到最大递归层数 " + req.maxRecursiveLevels
                    : hot.describe(threshold);
            node.put("strategy", "boundedBlockNestedLoop");
            node.put("fallbackReason", reason);
            bnlFallbacks++;
            hotKeyFallbacks++;
            stats.warn("分区 " + buildFile.label() + " 触发有界回退：" + reason);
            runBlockNestedLoop(buildFile, probeFile, sink, node);
            buildFile.delete();
            probeFile.delete();
            return;
        }

        // 递归再分区：目标仍是每个子分区 ≈ threshold 行；分区数封顶 1024，
        // 若热点导致超限，下一级的热点判定会直接转入有界回退。
        int subP = (int) Math.max(2, Math.min(1024,
                Math.ceil((double) bn / (double) threshold)));
        long seed = levelSeed(level);
        List<SpillFile> subBuild = newSpillFiles(subP, "L" + level + "-build-");
        List<SpillFile> subProbe = newSpillFiles(subP, "L" + level + "-probe-");
        long[] sbCount = new long[subP];
        long[] spCount = new long[subP];
        repartitionFile(buildFile, buildKeyIdx, subBuild, sbCount, seed);
        repartitionFile(probeFile, probeKeyIdx, subProbe, spCount, seed);
        node.put("strategy", "repartition");
        node.put("subPartitions", subP);
        node.put("level", level);
        spillWaves++;

        buildFile.delete();
        probeFile.delete();

        for (int i = 0; i < subP; i++) {
            subBuild.get(i).finishWriting();
            subProbe.get(i).finishWriting();
        }
        for (int i = 0; i < subP; i++) {
            StatsCollector.Node child = node.child("partitionProcess");
            child.put("partitionId", i);
            child.put("buildRows", sbCount[i]);
            child.put("probeRows", spCount[i]);
            processPartition(subBuild.get(i), subProbe.get(i), spCount[i], sink, level + 1, child);
        }
    }

    // -------------------------------------------------------- 有界回退：BNL

    /**
     * 分块嵌套循环。建表文件切块（每块 &lt;= threshold 行）装哈希表，
     * 对每块扫描一遍探测文件。LEFT 用磁盘位图标记探测行匹配情况，
     * 全部块结束后再扫一遍探测文件输出未匹配行。
     * 内存上界 ≈ threshold 行建表数据 + 哈希表开销；探测侧流式。
     */
    private void runBlockNestedLoop(SpillFile buildFile, SpillFile probeFile,
                                    RowSink sink, StatsCollector.Node node) {
        boolean leftJoin = req.joinType == JoinType.LEFT;
        long probeRows = probeFile.rowCount();
        ProbeMarker marker = leftJoin
                ? new DiskProbeMarker(spill, probeFile.label(), probeRows)
                : null;
        long blockCount = 0;
        long matchedPairs = 0;
        try {
            // 建表切块：顺序读，攒满 threshold 行就作为一个块做完整探测扫描。
            // 单块驻留内存行数上界 = threshold；探测侧始终流式。
            List<Row> block = new ArrayList<>((int) Math.min(threshold, Integer.MAX_VALUE));
            var buildIter = new LineRowIterator(buildFile);
            while (buildIter.hasNext()) {
                block.add(buildIter.next());
                if (block.size() >= threshold) {
                    blockCount++;
                    matchedPairs += probeOneBlock(block, probeFile, sink, marker);
                    block.clear();
                }
            }
            if (!block.isEmpty()) {
                blockCount++;
                matchedPairs += probeOneBlock(block, probeFile, sink, marker);
            }
            node.put("blocks", blockCount);
            node.put("matchedPairs", matchedPairs);

            if (leftJoin) {
                long[] unmatched = {0};
                long[] idx = {0};
                probeFile.forEachRow(pr -> {
                    long i = idx[0]++;
                    if (!marker.isMarked(i)) {
                        emit(sink, pr, null);
                        unmatched[0]++;
                    }
                });
                node.put("unmatchedProbeRows", unmatched[0]);
                node.put("marker", marker.kind());
            }
        } finally {
            if (marker != null) marker.close();
        }
    }

    private long probeOneBlock(List<Row> block, SpillFile probeFile,
                               RowSink sink, ProbeMarker marker) {
        Map<Key, List<Row>> table = new HashMap<>(Math.max(16, block.size() * 2));
        for (Row br : block) {
            table.computeIfAbsent(Key.ofRow(br, buildKeyIdx), k -> new ArrayList<>()).add(br);
        }
        boolean leftJoin = req.joinType == JoinType.LEFT;
        long[] idx = {0};
        long[] pairs = {0};
        probeFile.forEachRow(pr -> {
            long i = idx[0]++;
            Key k = Key.ofRow(pr, probeKeyIdx);
            List<Row> matches = table.get(k);
            if (matches != null) {
                for (Row br : matches) {
                    emit(sink, pr, br);
                    pairs[0]++;
                }
                if (leftJoin) marker.mark(i);
            }
        });
        return pairs[0];
    }

    // --------------------------------------------------------------- 热点判定

    /**
     * 流式统计建表分区中的不同键数与最高频键频次；
     * 不同键数超过 threshold+1 后提前停止（结论已定：不是“全同键”热点）。
     */
    private HotKeyInfo detectHotKey(SpillFile buildFile) {
        Map<Key, long[]> counts = new HashMap<>();
        long maxFreq = 0;
        Key maxKey = null;
        boolean capped = false;
        var iter = new LineRowIterator(buildFile);
        while (iter.hasNext()) {
            Row r = iter.next();
            Key k = Key.ofRow(r, buildKeyIdx);
            long[] c = counts.computeIfAbsent(k, x -> new long[1]);
            c[0]++;
            if (c[0] > maxFreq) {
                maxFreq = c[0];
                maxKey = k;
            }
            if (counts.size() > threshold + 1) {
                capped = true;
                break;
            }
        }
        return new HotKeyInfo(counts.size(), maxFreq, maxKey, capped, buildFile.rowCount());
    }

    private record HotKeyInfo(long distinctKeys, long maxFreq, Key maxKey,
                              boolean capped, long totalRows) {

        /**
         * 热点判定：
         *  - 分区全部为同一个键（distinctKeys == 1）；
         *  - 或最高频键本身已超过阈值（即使再分区，装该键的子分区仍超阈值）；
         *  capped（提前终止）说明不同键很多，绝不是全同键热点。
         */
        boolean isHot(long threshold) {
            if (capped && maxFreq <= threshold) return false;
            return distinctKeys == 1 || maxFreq > threshold;
        }

        String describe(long threshold) {
            if (distinctKeys == 1) return "分区为单一热点键 " + maxKey + "（" + totalRows + " 行），再分区无法打散";
            return "热点键 " + maxKey + " 频次 " + maxFreq + " 超过内存阈值 " + threshold
                    + "（分区共 " + totalRows + " 行）";
        }
    }

    // --------------------------------------------------------------- 分区写出

    /**
     * 分区路由。关键正确性约束：同一连接键在左右两侧必须落到同一分区，
     * 因此路由必须与连接判定（Key.equals / Value.equals，含 LONG/DOUBLE 跨型）一致，
     * 即直接使用 Key.hashCode，再叠加层种子做层间扰动。
     * 绝不能另写一套哈希（否则数值跨型相等的键会被拆到不同分区而漏匹配）。
     */
    private int route(Key k, int parts, long seed) {
        long h = HashMix.mix((long) k.hashCode() ^ seed);
        return HashMix.partition(h, parts);
    }

    private void partitionWrite(List<Row> rows, int[] keyIdx, List<SpillFile> parts,
                                long[] counts, long seed) {
        int p = parts.size();
        for (Row r : rows) {
            Key k = Key.ofRow(r, keyIdx);
            int part = route(k, p, seed);
            parts.get(part).appendRow(r);
            counts[part]++;
        }
    }

    private void repartitionFile(SpillFile src, int[] keyIdx, List<SpillFile> parts,
                                 long[] counts, long seed) {
        int p = parts.size();
        src.forEachRow(r -> {
            Key k = Key.ofRow(r, keyIdx);
            int part = route(k, p, seed);
            parts.get(part).appendRow(r);
            counts[part]++;
        });
    }

    private List<SpillFile> newSpillFiles(int n, String prefix) {
        List<SpillFile> files = new ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            files.add(spill.createFile(prefix + i));
        }
        return files;
    }

    private int effectivePartitions(long buildRows) {
        if (req.partitions > 0) return Math.max(1, req.partitions);
        long est = (long) Math.ceil((double) buildRows / (double) threshold);
        return (int) Math.max(2, Math.min(est, buildRows));
    }

    /** 每层用不同种子，尝试改变落点分布。 */
    private long levelSeed(int level) {
        return HashMix.mix(0x517cc1b727220a95L ^ (level * 0x9e3779b97f4a7c15L));
    }

    // --------------------------------------------------------------- 输出组装

    /**
     * 输出一行。探测行为左表时输出 probe+build；探测行为右表（INNER 小表在右时建表侧为左）
     * 输出 build+probe。LEFT 时 build 恒为右表。
     */
    private void emit(RowSink sink, Row probeRow, Row buildRow) {
        Row row;
        if (buildIsRight) {
            row = NestedLoopJoin.concat(probeRow, buildRow == null
                    ? NestedLoopJoin.nullRow(req.right.width()) : buildRow);
        } else {
            // INNER 且建表侧为左表：buildRow 必非 null（INNER 无补位）
            row = NestedLoopJoin.concat(buildRow, probeRow);
        }
        sink.accept(row);
        outputRows++;
    }

    // --------------------------------------------------------------- 统计导出

    private Map<String, Object> finalizeStats() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("outputRows", outputRows);
        m.put("nullKeyBuildRowsDropped", nullBuildRows);
        m.put("nullKeyProbeRows", nullProbeRows);
        m.put("inMemoryPartitions", inMemoryPartitions);
                m.put("spillWaves", spillWaves);
        m.put("boundedFallbacks", bnlFallbacks);
        m.put("hotKeyFallbacks", hotKeyFallbacks);
        m.put("spillPeakBytes", spill.peakBytes());
        m.put("spillLiveBytesAtEnd", spill.liveBytes());
        m.put("diskQuotaBytes", req.diskQuotaBytes);
        m.put("warnings", new ArrayList<>(stats.warnings()));
        root.put("stats", m);
        return m;
    }

    private List<String> keyColumnNames() {
        List<String> names = new ArrayList<>();
        for (var p : req.keyPairs) names.add(p.leftColumn() + "=" + p.rightColumn());
        return Collections.unmodifiableList(names);
    }

    /** 建表文件的逐块流式读取包装（底层逐行 I/O，不在内存堆积整个分区）。 */
    private static final class LineRowIterator {
        private final java.util.Iterator<Row> it;

        LineRowIterator(SpillFile file) {
            this.it = file.streamingIterator();
        }

        boolean hasNext() { return it.hasNext(); }

        Row next() { return it.next(); }
    }
}
