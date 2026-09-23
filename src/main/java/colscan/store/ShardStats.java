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
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 一个分片的统计文件（shard-N/stats.json）。
 * hasStats=false 表示该分片“统计缺失”：裁剪器必须保守地扫描它。
 * 物理上缺失 stats.json 也等价于统计缺失。
 */
public final class ShardStats {

    public boolean hasStats;
    public long rowCount;
    public Map<String, ColumnStats> columns = new LinkedHashMap<>();

    public static ShardStats compute(long rowCount, Map<String, String> types,
                                     Map<String, Object[]> data) {
        ShardStats s = new ShardStats();
        s.hasStats = true;
        s.rowCount = rowCount;
        for (Map.Entry<String, String> e : types.entrySet()) {
            String col = e.getKey();
            Object[] values = data.get(col);
            long nulls = 0;
            Number min = null;
            Number max = null;
            if (values != null) {
                for (Object v : values) {
                    if (v == null) {
                        nulls++;
                    } else if (min == null) {
                        min = (Number) v;
                        max = (Number) v;
                    } else if (Types.compare(e.getValue(), v, min) < 0) {
                        min = (Number) v;
                    } else if (Types.compare(e.getValue(), v, max) > 0) {
                        max = (Number) v;
                    }
                }
            }
            s.columns.put(col, new ColumnStats(nulls, min, max));
        }
        return s;
    }

    private Map<String, Object> toJson() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("hasStats", hasStats);
        m.put("rowCount", rowCount);
        Map<String, Object> cols = new LinkedHashMap<>();
        for (Map.Entry<String, ColumnStats> e : columns.entrySet()) {
            cols.put(e.getKey(), e.getValue().toJson());
        }
        m.put("columns", cols);
        return m;
    }

    public static void write(Path statsFile, ShardStats stats) throws IOException {
        Files.createDirectories(statsFile.getParent());
        try (OutputStream raw = Files.newOutputStream(statsFile);
             BufferedOutputStream out = new BufferedOutputStream(raw)) {
            out.write(Json.pretty(stats.toJson()).getBytes(StandardCharsets.UTF_8));
        }
    }

    /** 读取结果，带字节统计。文件不存在时返回 null（即“统计缺失”）。 */
    public static final class LoadResult {
        public final ShardStats stats;
        public final long bytesRead;

        LoadResult(ShardStats stats, long bytesRead) {
            this.stats = stats;
            this.bytesRead = bytesRead;
        }
    }

    @SuppressWarnings("unchecked")
    public static LoadResult load(Path statsFile) throws IOException {
        if (!Files.isRegularFile(statsFile)) return new LoadResult(null, 0);
        StringBuilder sb = new StringBuilder();
        long bytes = 0;
        try (InputStream raw = Files.newInputStream(statsFile);
             BufferedInputStream buf = new BufferedInputStream(raw);
             CountingInputStream in = new CountingInputStream(buf)) {
            byte[] chunk = new byte[4096];
            int n;
            while ((n = in.read(chunk)) != -1) {
                sb.append(new String(chunk, 0, n, StandardCharsets.UTF_8));
                bytes += n;
            }
        }
        Map<String, Object> m = Json.parseObject(sb.toString());
        ShardStats s = new ShardStats();
        s.hasStats = Boolean.TRUE.equals(m.get("hasStats"));
        s.rowCount = m.get("rowCount") == null ? -1
                : ((Number) m.get("rowCount")).longValue();
        Object colsObj = m.get("columns");
        if (colsObj instanceof Map) {
            for (Map.Entry<String, Object> e : ((Map<String, Object>) colsObj).entrySet()) {
                s.columns.put(e.getKey(),
                        ColumnStats.fromJson((Map<String, Object>) e.getValue()));
            }
        }
        return new LoadResult(s, bytes);
    }
}
