package colscan;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * One columnar shard ("row group") held in memory and persisted to a columnar
 * binary file. Each column stores only LONG values plus a presence bit per row
 * (NULLs are encoded as absent bits, never as the number 0).
 *
 * File layout (little-endian, see ColumnFile):
 *   magic "CSHF" | version u16
 *   per column: present-bits bytes (ceil(rows/8)) then rows * int64 values
 *   footer JSON (UTF-8) | footer length int32 | magic "CSFT"
 */
public final class Shard {

    /** Statistics for a single column of one shard. Any field may be missing. */
    public static final class Stats {
        public final Long min;    // null => statistic ABSENT
        public final Long max;    // null => statistic ABSENT
        public final Long nullCount; // null => statistic ABSENT

        public Stats(Long min, Long max, Long nullCount) {
            this.min = min;
            this.max = max;
            this.nullCount = nullCount;
        }

        public boolean hasMinMax() {
            return min != null && max != null;
        }
    }

    public final String table;
    public final String shardId;
    public final int rowCount;
    private final Map<String, long[]> columns = new LinkedHashMap<>();
    private final Map<String, boolean[]> presence = new LinkedHashMap<>();
    final Map<String, Stats> stats = new LinkedHashMap<>();

    public Shard(String table, String shardId,
                 Map<String, long[]> columns, Map<String, boolean[]> presence) {
        this.table = table;
        this.shardId = shardId;        this.rowCount = firstLength(columns, presence);
        for (Map.Entry<String, long[]> e : columns.entrySet()) {
            long[] data = e.getValue();
            boolean[] pres = presence.get(e.getKey());
            if (pres == null) {
                throw new IllegalArgumentException("missing presence array for column " + e.getKey());
            }
            if (data.length != rowCount || pres.length != rowCount) {
                throw new IllegalArgumentException("column " + e.getKey() + " length mismatch");
            }
            this.columns.put(e.getKey(), data);
            this.presence.put(e.getKey(), pres);
            this.stats.put(e.getKey(), computeStats(data, pres));
        }
    }

    private Shard(String table, String shardId, int rowCount,
                  Map<String, long[]> columns, Map<String, boolean[]> presence,
                  Map<String, Stats> stats) {
        this.table = table;
        this.shardId = shardId;
        this.rowCount = rowCount;
        this.columns.putAll(columns);
        this.presence.putAll(presence);
        this.stats.putAll(stats);
    }

    private static int firstLength(Map<String, long[]> columns, Map<String, boolean[]> presence) {
        if (columns.isEmpty()) {
            return 0;
        }
        String first = columns.keySet().iterator().next();
        return columns.get(first).length;
    }

    /** Compute honest statistics from the data. All-NULL column => min/max absent. */
    static Stats computeStats(long[] data, boolean[] presence) {
        Long min = null;
        Long max = null;
        long nulls = 0;
        for (int i = 0; i < data.length; i++) {
            if (!presence[i]) {
                nulls++;
            } else {
                if (min == null || data[i] < min) min = data[i];
                if (max == null || data[i] > max) max = data[i];
            }
        }
        return new Stats(min, max, nulls);
    }

    public java.util.List<String> columnNames() {
        return new java.util.ArrayList<>(columns.keySet());
    }

    public long[] data(String col) {
        return columns.get(col);
    }

    public boolean[] presence(String col) {
        return presence.get(col);
    }

    public Stats stats(String col) {
        return stats.get(col);
    }

    /**
     * Return a copy of this shard with per-column statistics deliberately
     * stripped for the given columns (simulating corrupt / unwritten stats
     * that force a scan). Data is unchanged.
     */
    public Shard withMissingStats(java.util.Collection<String> columnsToStrip) {
        Map<String, Stats> newStats = new LinkedHashMap<>(stats);
        for (String c : columnsToStrip) {
            if (!newStats.containsKey(c)) {
                throw new IllegalArgumentException("unknown column: " + c);
            }
            newStats.put(c, new Stats(null, null, null));
        }
        return new Shard(table, shardId, rowCount, columns, presence, newStats);
    }
}
