package colscan;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.stream.Stream;

/**
 * In-process catalog: table name -> ordered list of on-disk columnar shards.
 * Ingested rows are turned into column arrays with explicit presence bits
 * (NULL is a missing bit, not a zero), written to disk via {@link ColumnFile}.
 * A shard may be persisted with deliberately missing statistics to simulate
 * unwritten/corrupt stats — pruning must then conservatively scan it.
 */
public final class Catalog {

    private final Path dataDir;
    private final Map<String, List<ColumnFile.Handle>> tables = new TreeMap<>();

    public Catalog(Path dataDir) throws IOException {
        this.dataDir = dataDir;
        Files.createDirectories(dataDir);
        loadFromDisk();
    }

    private void loadFromDisk() throws IOException {
        try (Stream<Path> files = Files.list(dataDir)) {
            List<Path> csc = files.filter(p -> p.toString().endsWith(".csc")).toList();
            for (Path p : csc) {
                ColumnFile.Handle h = ColumnFile.open(p);
                tables.computeIfAbsent(h.table, k -> new ArrayList<>()).add(h);
            }
        }
        for (List<ColumnFile.Handle> list : tables.values()) {
            list.sort((a, b) -> a.shardId.compareTo(b.shardId));
        }
    }

    public Path dataDir() {
        return dataDir;
    }

    public synchronized List<String> tableNames() {
        return new ArrayList<>(tables.keySet());
    }

    public synchronized List<ColumnFile.Handle> shards(String table) {
        List<ColumnFile.Handle> l = tables.get(table);
        return l == null ? List.of() : new ArrayList<>(l);
    }

    /**
     * Ingest a shard.
     *
     * @param rows    list of rows; each row maps column name to Long or null
     * @param missingStats columns whose min/max/nullCount stats are omitted on disk
     */
    public synchronized ColumnFile.Handle ingest(String table, String shardId,
                                                  List<Map<String, Object>> rows,
                                                  List<String> missingStats) throws IOException {
        if (shardId == null || shardId.isBlank()) {
            throw new IllegalArgumentException("shardId required");
        }
        List<ColumnFile.Handle> existing = tables.computeIfAbsent(table, k -> new ArrayList<>());
        for (ColumnFile.Handle h : existing) {
            if (h.shardId.equals(shardId)) {
                throw new IllegalArgumentException("shard already exists: " + shardId
                        + " (delete the table first to re-ingest)");
            }
        }
        if (rows.isEmpty()) {
            throw new IllegalArgumentException("cannot ingest empty shard");
        }

        // Column schema = union of keys, first row's order preserved.
        List<String> columns = new ArrayList<>();
        for (Map<String, Object> row : rows) {
            for (String k : row.keySet()) {
                if (!columns.contains(k)) columns.add(k);
            }
        }
        int n = rows.size();
        Map<String, long[]> data = new LinkedHashMap<>();
        Map<String, boolean[]> presence = new LinkedHashMap<>();
        for (String c : columns) {
            data.put(c, new long[n]);
            presence.put(c, new boolean[n]);
        }
        for (int r = 0; r < n; r++) {
            Map<String, Object> row = rows.get(r);
            for (String c : columns) {
                Object v = row.get(c);
                if (v != null) {
                    long lv = v instanceof Number ? ((Number) v).longValue() : Long.parseLong(v.toString());
                    data.get(c)[r] = lv;
                    presence.get(c)[r] = true;
                }
                // absent key or explicit null => presence stays false (genuine NULL)
            }
        }

        Shard shard = new Shard(table, shardId, data, presence);
        if (missingStats != null && !missingStats.isEmpty()) {
            shard = shard.withMissingStats(missingStats);
        }
        Path path = ColumnFile.write(dataDir, shard);
        ColumnFile.Handle h = ColumnFile.open(path);
        existing.add(h);
        existing.sort((a, b) -> a.shardId.compareTo(b.shardId));
        return h;
    }

    public synchronized void dropTable(String table) throws IOException {
        List<ColumnFile.Handle> list = tables.remove(table);
        if (list == null) return;
        for (ColumnFile.Handle h : list) {
            Files.deleteIfExists(h.path);
        }
    }
}
