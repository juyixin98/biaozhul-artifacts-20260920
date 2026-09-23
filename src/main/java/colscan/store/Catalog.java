package colscan.store;

import colscan.json.Json;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 磁盘布局：
 * <pre>
 * dataDir/
 *   &lt;table&gt;/
 *     table.json                 {name, columns: {col: TYPE}, shardCount}
 *     shard-0/
 *       shard.json               {rowCount}
 *       stats.json               统计（可能缺失或 hasStats=false）
 *       &lt;col&gt;.col                二进制列文件
 * </pre>
 * 写操作（ingest）在实例上加锁；读操作无状态、可并发。
 */
public final class Catalog {

    public static final String TABLE_META = "table.json";
    public static final String SHARD_META = "shard.json";
    public static final String STATS = "stats.json";
    public static final String COL_SUFFIX = ".col";

    public static final class Table {
        public final String name;
        public final Map<String, String> columns; // 有序
        public final int shardCount;

        Table(String name, Map<String, String> columns, int shardCount) {
            this.name = name;
            this.columns = columns;
            this.shardCount = shardCount;
        }
    }

    public static final class IngestResult {
        public final int shardIndex;
        public final long rowCount;
        public final boolean statsWritten;

        IngestResult(int shardIndex, long rowCount, boolean statsWritten) {
            this.shardIndex = shardIndex;
            this.rowCount = rowCount;
            this.statsWritten = statsWritten;
        }
    }

    private final Path dataDir;

    public Catalog(Path dataDir) throws IOException {
        this.dataDir = dataDir;
        Files.createDirectories(dataDir);
    }

    public Path dataDir() {
        return dataDir;
    }

    private Path tableDir(String table) {
        return dataDir.resolve(table);
    }

    private Path tableMetaFile(String table) {
        return tableDir(table).resolve(TABLE_META);
    }

    public Path shardDir(String table, int shard) {
        return tableDir(table).resolve(String.format("shard-%d", shard));
    }

    public Path columnFile(String table, int shard, String column) {
        return shardDir(table, shard).resolve(column + COL_SUFFIX);
    }

    public Path statsFile(String table, int shard) {
        return shardDir(table, shard).resolve(STATS);
    }

    // ---------------- 表元数据 ----------------

    @SuppressWarnings("unchecked")
    private Table loadTable(String table) throws IOException {
        Path f = tableMetaFile(table);
        if (!Files.isRegularFile(f)) return null;
        try (InputStream raw = Files.newInputStream(f);
             BufferedInputStream buf = new BufferedInputStream(raw)) {
            byte[] all = buf.readAllBytes();
            Map<String, Object> m = Json.parseObject(new String(all, StandardCharsets.UTF_8));
            Map<String, String> cols = new LinkedHashMap<>();
            Object columns = m.get("columns");
            if (columns instanceof Map) {
                for (Map.Entry<String, Object> e : ((Map<String, Object>) columns).entrySet()) {
                    cols.put(e.getKey(), String.valueOf(e.getValue()));
                }
            }
            Number sc = (Number) m.get("shardCount");
            return new Table(table, cols, sc == null ? 0 : sc.intValue());
        }
    }

    public Table getTable(String table) throws IOException {
        return loadTable(table);
    }

    public List<String> listTables() throws IOException {
        List<String> names = new ArrayList<>();
        if (!Files.isDirectory(dataDir)) return names;
        try (var stream = Files.list(dataDir)) {
            stream.filter(Files::isDirectory)
                    .filter(d -> Files.isRegularFile(d.resolve(TABLE_META)))
                    .forEach(d -> names.add(d.getFileName().toString()));
        }
        Collections.sort(names);
        return names;
    }

    private static void writeTableMeta(Path f, String name, Map<String, String> columns,
                                       int shardCount) throws IOException {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", name);
        m.put("columns", new TreeMap<>(columns));
        m.put("shardCount", shardCount);
        try (OutputStream raw = Files.newOutputStream(f);
             BufferedOutputStream out = new BufferedOutputStream(raw)) {
            out.write(Json.pretty(m).getBytes(StandardCharsets.UTF_8));
        }
    }

    // ---------------- 写入 ----------------

    /**
     * 追加一个分片。
     *
     * @param columnTypes 调用方解析好的列类型（新表即建表 schema；老表必须与现有 schema 一致）
     * @param data        每个列一列值（null 元素即 NULL）；缺列按全 NULL 补齐
     * @param computeStats false 时只写一个 hasStats=false 的占位 stats.json，模拟统计缺失
     */
    public synchronized IngestResult appendShard(String table,
                                                 Map<String, String> columnTypes,
                                                 Map<String, Object[]> data,
                                                 boolean computeStats) throws IOException {
        if (table == null || !table.matches("[A-Za-z][A-Za-z0-9_]*")) {
            throw new IllegalArgumentException(
                    "非法表名（允许字母/数字/下划线，字母开头）: " + table);
        }
        if (columnTypes.isEmpty()) {
            throw new IllegalArgumentException("至少需要一个列");
        }
        for (Map.Entry<String, String> e : columnTypes.entrySet()) {
            if (!e.getKey().matches("[A-Za-z][A-Za-z0-9_]*")) {
                throw new IllegalArgumentException("非法列名: " + e.getKey());
            }
            if (!Types.isValid(e.getValue())) {
                throw new IllegalArgumentException("不支持的列类型: " + e.getValue()
                        + "（当前仅支持 LONG / DOUBLE）");
            }
        }

        int rows = data.values().iterator().next().length;
        if (rows == 0) {
            throw new IllegalArgumentException("空分片（rows=0）不允许写入");
        }
        for (Map.Entry<String, Object[]> e : data.entrySet()) {
            if (e.getValue().length != rows) {
                throw new IllegalArgumentException("列 " + e.getKey()
                        + " 的行数与其他列不一致");
            }
        }

        Table existing = loadTable(table);
        Map<String, String> schema;
        int shardIndex;
        if (existing == null) {
            schema = new LinkedHashMap<>(columnTypes);
            shardIndex = 0;
            Files.createDirectories(tableDir(table));
        } else {
            if (!existing.columns.equals(new LinkedHashMap<>(columnTypes))) {
                throw new IllegalArgumentException(
                        "schema 与已有表不一致。已有: " + existing.columns
                                + "，本次: " + columnTypes);
            }
            schema = existing.columns;
            shardIndex = existing.shardCount;
        }

        Path shardDir = shardDir(table, shardIndex);
        Files.createDirectories(shardDir);

        // 缺列补全 NULL（不会被当成 0）
        Map<String, Object[]> fullData = new LinkedHashMap<>();
        for (String col : schema.keySet()) {
            fullData.put(col, data.getOrDefault(col, new Object[rows]));
        }
        for (Map.Entry<String, Object[]> e : fullData.entrySet()) {
            ColumnFile.write(columnFile(table, shardIndex, e.getKey()),
                    schema.get(e.getKey()), e.getValue());
        }

        ShardStats stats = ShardStats.compute(rows, schema, fullData);
        if (!computeStats) {
            stats.hasStats = false;
            stats.columns.clear(); // 统计缺失：不暴露任何 min/max/NULL 信息
        }
        ShardStats.write(statsFile(table, shardIndex), stats);

        Map<String, Object> shardMeta = new LinkedHashMap<>();
        shardMeta.put("rowCount", rows);
        try (OutputStream raw = Files.newOutputStream(shardDir.resolve(SHARD_META));
             BufferedOutputStream out = new BufferedOutputStream(raw)) {
            out.write(Json.pretty(shardMeta).getBytes(StandardCharsets.UTF_8));
        }

        writeTableMeta(tableMetaFile(table), table, schema, shardIndex + 1);
        return new IngestResult(shardIndex, rows, computeStats);
    }

    /** 读取分片 rowCount（shard.json）。 */
    public long shardRowCount(String table, int shard) throws IOException {
        Path f = shardDir(table, shard).resolve(SHARD_META);
        try (InputStream raw = Files.newInputStream(f);
             BufferedInputStream buf = new BufferedInputStream(raw)) {
            byte[] all = buf.readAllBytes();
            Map<String, Object> m = Json.parseObject(new String(all, StandardCharsets.UTF_8));
            return ((Number) m.get("rowCount")).longValue();
        }
    }
}
