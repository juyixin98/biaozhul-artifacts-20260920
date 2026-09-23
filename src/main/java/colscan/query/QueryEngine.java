package colscan.query;

import colscan.store.Catalog;
import colscan.store.ColumnFile;
import colscan.store.ColumnStats;
import colscan.store.ShardStats;
import colscan.store.Types;

import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 查询引擎：同一份查询分别跑“裁剪”与“全扫描”，返回结果与字节账，供对照。 */
public final class QueryEngine {

    public enum Mode { PRUNED, FULL_SCAN }

    /** 单个分片的执行明细。 */
    public static final class ShardOutcome {
        public final int shard;
        public final boolean scanned;
        public final String reason;
        public final long rowsInShard;
        public final long matchedRows;
        public final long statsBytesRead;
        public final long dataBytesRead;
        public final List<String> columnsRead;

        public ShardOutcome(int shard, boolean scanned, String reason, long rowsInShard,
                            long matchedRows, long statsBytesRead, long dataBytesRead,
                            List<String> columnsRead) {
            this.shard = shard;
            this.scanned = scanned;
            this.reason = reason;
            this.rowsInShard = rowsInShard;
            this.matchedRows = matchedRows;
            this.statsBytesRead = statsBytesRead;
            this.dataBytesRead = dataBytesRead;
            this.columnsRead = columnsRead;
        }

        public long totalBytesRead() {
            return statsBytesRead + dataBytesRead;
        }
    }

    /** 一种模式（裁剪 / 全扫描）的完整结果。 */
    public static final class ModeResult {
        public final Mode mode;
        public final long totalRows;
        public final long matchedRows;
        public final int scannedShards;
        public final int prunedShards;
        public final long statsBytes;
        public final long dataBytes;
        public final Map<String, Object> aggregates;
        public final List<Map<String, Object>> rows;
        public final List<ShardOutcome> shards;

        public ModeResult(Mode mode, long totalRows, long matchedRows, int scannedShards,
                   int prunedShards, long statsBytes, long dataBytes,
                   Map<String, Object> aggregates, List<Map<String, Object>> rows,
                   List<ShardOutcome> shards) {
            this.mode = mode;
            this.totalRows = totalRows;
            this.matchedRows = matchedRows;
            this.scannedShards = scannedShards;
            this.prunedShards = prunedShards;
            this.statsBytes = statsBytes;
            this.dataBytes = dataBytes;
            this.aggregates = aggregates;
            this.rows = rows;
            this.shards = shards;
        }

        public long statsBytesRead() { return statsBytes; }
        public long dataBytesRead() { return dataBytes; }
        public long totalBytesRead() { return statsBytes + dataBytes; }
    }

    private final Catalog catalog;

    public QueryEngine(Catalog catalog) {
        this.catalog = catalog;
    }

    /** 执行查询，返回 {PRUNED, FULL_SCAN}。 */
    public Map<Mode, ModeResult> execute(QueryRequest req) throws IOException {
        Map<Mode, ModeResult> out = new LinkedHashMap<>();
        out.put(Mode.PRUNED, runMode(req, Mode.PRUNED));
        out.put(Mode.FULL_SCAN, runMode(req, Mode.FULL_SCAN));
        return out;
    }

    private ModeResult runMode(QueryRequest req, Mode mode) throws IOException {
        boolean useStats = mode == Mode.PRUNED;

        // 本次查询必需的列（过滤列 + 聚合列 + 投影列）。
        // 两种模式下，每个“被扫描”的分片都读取完全相同的列集合，
        // 因此两种模式的字节差异只来自分片跳过（以及裁剪模式多读的 stats.json），
        // 保证对照公平。
        List<String> neededColumns = new ArrayList<>();
        if (req.filter != null && !neededColumns.contains(req.filter.column)) {
            neededColumns.add(req.filter.column);
        }
        for (AggSpec agg : req.aggs) {
            if (agg.column != null && !neededColumns.contains(agg.column)) {
                neededColumns.add(agg.column);
            }
        }
        if (req.returnRows) {
            for (String col : req.schema.keySet()) {
                if (!neededColumns.contains(col)) neededColumns.add(col);
            }
        }

        long totalRows = 0;
        long matchedRows = 0;
        long statsBytes = 0;
        long dataBytes = 0;
        int scanned = 0;
        int pruned = 0;

        Map<String, Acc> globalAccs = new LinkedHashMap<>();
        for (AggSpec agg : req.aggs) {
            globalAccs.put(agg.alias, new Acc(agg, agg.column == null
                    || Types.LONG.equals(req.schema.get(agg.column))));
        }

        List<Map<String, Object>> rows = new ArrayList<>();
        List<ShardOutcome> outcomes = new ArrayList<>();

        for (int sIt = 0; sIt < req.shardCount; sIt++) {
            final int s = sIt;
            long shardRowsLong = catalog.shardRowCount(req.table, s);
            int shardRows = Math.toIntExact(shardRowsLong);
            totalRows += shardRows;

            boolean scan;
            String reason;
            long shardStatsBytes = 0;

            if (req.filter == null) {
                scan = true;
                reason = "scan:no_filter";
            } else if (!useStats) {
                scan = true;
                reason = "scan:full_scan_mode";
            } else {
                ShardStats.LoadResult loaded =
                        ShardStats.load(catalog.statsFile(req.table, s));
                shardStatsBytes = loaded.bytesRead;
                statsBytes += shardStatsBytes;
                ShardStats ss = loaded.stats;
                ColumnStats filterStats = (ss != null && ss.hasStats)
                        ? ss.columns.get(req.filter.column) : null;
                if (ss == null || !ss.hasStats || filterStats == null) {
                    // 统计文件物理缺失 / hasStats=false / 该列统计缺失：一律保守扫描
                    scan = true;
                    reason = Pruner.SCAN_STATS_MISSING;
                } else {
                    Pruner.Decision d = Pruner.decide(filterStats, req.filter);
                    scan = !d.prune;
                    reason = d.reason;
                }
            }

            long shardMatched = 0;
            long shardDataBytes = 0;
            List<String> columnsRead = new ArrayList<>();

            if (!scan) {
                pruned++;
            } else {
                scanned++;

                // 读取该分片本次查询的全部必需列（每列文件只读一次，字节账不重不漏）
                Map<String, Object[]> loadedColumns = new LinkedHashMap<>();
                for (String col : neededColumns) {
                    ColumnFile.ReadResult rr =
                            ColumnFile.read(catalog.columnFile(req.table, s, col));
                    shardDataBytes += rr.bytesRead;
                    columnsRead.add(col);
                    loadedColumns.put(col, rr.values);
                }

                Object[] filterValues = req.filter == null ? null
                        : loadedColumns.get(req.filter.column);
                boolean[] match = new boolean[shardRows];
                for (int i = 0; i < shardRows; i++) {
                    boolean ok = req.filter == null || req.filter.matches(filterValues[i]);
                    match[i] = ok;
                    if (ok) shardMatched++;
                }

                for (Acc acc : globalAccs.values()) {
                    if (acc.spec.func.equals(AggSpec.COUNT)) {
                        for (int i = 0; i < shardRows; i++) {
                            if (match[i]) acc.addRow();
                        }
                    } else {
                        Object[] vals = loadedColumns.get(acc.spec.column);
                        for (int i = 0; i < shardRows; i++) {
                            if (match[i]) acc.addValue(vals[i] == null ? null : (Number) vals[i]);
                        }
                    }
                }

                if (req.returnRows && rows.size() < req.rowLimit && shardMatched > 0) {
                    for (int i = 0; i < shardRows && rows.size() < req.rowLimit; i++) {
                        if (!match[i]) continue;
                        Map<String, Object> row = new LinkedHashMap<>();
                        for (String col : req.schema.keySet()) {
                            row.put(col, loadedColumns.get(col)[i]); // null 原样输出
                        }
                        rows.add(row);
                    }
                }

                matchedRows += shardMatched;
                dataBytes += shardDataBytes;
            }

            outcomes.add(new ShardOutcome(s, scan, reason, shardRows,
                    scan ? shardMatched : 0, shardStatsBytes,
                    scan ? shardDataBytes : 0, columnsRead));
        }

        Map<String, Object> aggOut = new LinkedHashMap<>();
        for (Map.Entry<String, Acc> e : globalAccs.entrySet()) {
            aggOut.put(e.getKey(), e.getValue().value());
        }

        return new ModeResult(mode, totalRows, matchedRows, scanned, pruned,
                statsBytes, dataBytes, aggOut, rows, outcomes);
    }
}
