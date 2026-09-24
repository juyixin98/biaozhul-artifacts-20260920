package bitserver;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 位图索引服务（线程安全，所有公开方法 synchronized）。
 *
 * 状态：
 *   index  —— 不可变的多维位图索引（reload 时整体替换）；
 *   alive  —— 存活掩码，位 i=1 表示行 ID i 当前存活。删除=清位，恢复=置位，
 *             行 ID 本身永不变化。
 */
public final class IndexService {

    private static final class State {
        final BitmapIndex index;
        final Bitmap alive;
        final String source;       // 数据来源描述（文件路径或 "json"）
        final long rawDataBytes;   // 原始数据集字节（用于空间统计）
        final long loadedAtMillis;

        State(BitmapIndex index, String source, long rawDataBytes, long loadedAtMillis) {
            this.index = index;
            this.alive = Bitmap.full(index.rowCount());
            this.source = source;
            this.rawDataBytes = rawDataBytes;
            this.loadedAtMillis = loadedAtMillis;
        }
    }

    private State state;

    public static final class QueryResult {
        public final int[] ids;
        public final int matchedAlive;
        public final int aliveCount;
        public final int totalCount;
        public final boolean truncated;

        QueryResult(int[] ids, int matchedAlive, int aliveCount, int totalCount, boolean truncated) {
            this.ids = ids;
            this.matchedAlive = matchedAlive;
            this.aliveCount = aliveCount;
            this.totalCount = totalCount;
            this.truncated = truncated;
        }
    }

    public static final class ChangeResult {
        public final int changed;
        public final int aliveCount;
        public final int totalCount;

        ChangeResult(int changed, int aliveCount, int totalCount) {
            this.changed = changed;
            this.aliveCount = aliveCount;
            this.totalCount = totalCount;
        }
    }

    public synchronized boolean isLoaded() {
        return state != null;
    }

    /** 从 CSV 文件加载（启动时自动加载也走这里）。 */
    public synchronized void loadCsvFile(Path file) {
        String content;
        byte[] bytes;
        try {
            bytes = Files.readAllBytes(file);
            content = new String(bytes, StandardCharsets.UTF_8);
        } catch (Exception e) {
            throw new IllegalArgumentException("读取 CSV 文件失败: " + file + " (" + e.getMessage() + ")");
        }
        Csv.Table table = Csv.parse(content);
        BitmapIndex index = BitmapIndex.build(table.headers, table.rows);
        state = new State(index, file.toString(), bytes.length, System.currentTimeMillis());
    }

    /**
     * 通过 HTTP 请求体加载。
     * 支持：
     *   {"csv":"city,grade\n..."}                      直接内嵌 CSV
     *   {"csvPath":"/data/x.csv"}                      服务端文件路径
     *   {"columns":["city",...], "rows":[["BJ",...]]}  结构化 JSON 数据
     */
    @SuppressWarnings("unchecked")
    public synchronized int load(Map<String, Object> body) {
        Object csvPath = body.get("csvPath");
        if (csvPath instanceof String) {
            if (body.size() > 1) {
                throw new IllegalArgumentException("csvPath 必须单独提供，不能与其它字段混用");
            }
            loadCsvFile(Path.of((String) csvPath));
        } else if (body.get("csv") instanceof String csv) {
            if (body.size() > 1) {
                throw new IllegalArgumentException("csv 必须单独提供，不能与其它字段混用");
            }
            Csv.Table table = Csv.parse(csv);
            BitmapIndex index = BitmapIndex.build(table.headers, table.rows);
            byte[] raw = csv.getBytes(StandardCharsets.UTF_8);
            state = new State(index, "inline-csv", raw.length, System.currentTimeMillis());
        } else if (body.get("columns") instanceof List<?> cols && body.get("rows") instanceof List<?> rows) {
            loadJsonRows(cols, rows);
        } else {
            throw new IllegalArgumentException(
                    "加载请求必须包含 csv（内嵌 CSV 字符串）、csvPath（服务器文件路径）或 columns+rows（JSON 数据）");
        }
        return state.index.rowCount();
    }

    private void loadJsonRows(List<?> colsRaw, List<?> rowsRaw) {
        List<String> columns = new ArrayList<>();
        for (Object c : colsRaw) {
            if (!(c instanceof String) || ((String) c).isEmpty()) {
                throw new IllegalArgumentException("columns 必须全部是非空字符串");
            }
            if (columns.contains(c)) {
                throw new IllegalArgumentException("列名重复: " + c);
            }
            columns.add((String) c);
        }
        if (columns.isEmpty()) {
            throw new IllegalArgumentException("至少需要一列");
        }
        List<List<String>> rows = new ArrayList<>();
        long rawBytes = 0;
        int rowIdx = 0;
        for (Object ro : rowsRaw) {
            if (!(ro instanceof List<?> row)) {
                throw new IllegalArgumentException("rows 第 " + rowIdx + " 项必须是数组");
            }
            if (row.size() != columns.size()) {
                throw new IllegalArgumentException(
                        "第 " + rowIdx + " 行有 " + row.size() + " 列，与列数 " + columns.size() + " 不一致");
            }
            List<String> strings = new ArrayList<>(row.size());
            for (Object v : row) {
                if (v == null) {
                    throw new IllegalArgumentException("第 " + rowIdx + " 行存在 null，请使用空字符串表示缺失值");
                }
                String sv = QueryEngine.normalizeValue(v);
                strings.add(sv);
                rawBytes += sv.getBytes(StandardCharsets.UTF_8).length;
            }
            rows.add(strings);
            rowIdx++;
        }
        BitmapIndex index = BitmapIndex.build(columns, rows);
        state = new State(index, "json", rawBytes, System.currentTimeMillis());
    }

    /** 执行布尔查询，返回命中的存活行 ID（升序）。 */
    @SuppressWarnings("unchecked")
    public synchronized QueryResult query(Map<String, Object> body) {
        requireLoaded();
        Object where = body.get("where");
        if (!(where instanceof Map<?, ?>)) {
            throw new IllegalArgumentException("查询请求必须包含 where 表达式对象");
        }
        Object limitObj = body.get("limit");
        int limit = Integer.MAX_VALUE;
        if (limitObj != null) {
            if (!(limitObj instanceof Long) || (Long) limitObj < 0) {
                throw new IllegalArgumentException("limit 必须是非负整数");
            }
            limit = (int) Math.min((Long) limitObj, Integer.MAX_VALUE);
        }
        QueryEngine engine = new QueryEngine(state.index);
        Bitmap result = engine.query((Map<String, Object>) where, state.alive);
        int matched = result.cardinality();
        int[] all = result.toArray();
        boolean truncated = all.length > limit;
        int[] ids = truncated ? java.util.Arrays.copyOf(all, limit) : all;
        return new QueryResult(ids, matched, state.alive.cardinality(), state.index.rowCount(), truncated);
    }

    /** 按行 ID 或表达式删除；返回实际状态发生变化的行数。 */
    public synchronized ChangeResult delete(List<Object> ids, Map<String, Object> where) {
        return applyMask(ids, where, true);
    }

    /** 恢复（取消删除）。 */
    public synchronized ChangeResult restore(List<Object> ids, Map<String, Object> where) {
        return applyMask(ids, where, false);
    }

    @SuppressWarnings("unchecked")
    private ChangeResult applyMask(List<Object> idsRaw, Map<String, Object> where, boolean deleting) {
        requireLoaded();
        if ((idsRaw == null) == (where == null)) {
            throw new IllegalArgumentException(deleting
                    ? "删除请求必须且只能提供 ids 或 where 之一"
                    : "恢复请求必须且只能提供 ids 或 where 之一");
        }
        Bitmap target = new Bitmap(state.index.rowCount());
        if (idsRaw != null) {
            int n = state.index.rowCount();
            int i = 0;
            for (Object o : idsRaw) {
                if (!(o instanceof Long) || (Long) o < 0 || (Long) o >= n) {
                    throw new IllegalArgumentException("ids[" + i + "]=" + o + " 越界，合法范围 [0," + (n - 1) + "]");
                }
                target.set(((Long) o).intValue());
                i++;
            }
        } else {
            QueryEngine engine = new QueryEngine(state.index);
            // 删除选择集按表达式在“全数据集”上求值（允许选中已删除行，幂等），
            // 再由存活掩码决定哪些行真正发生状态变化。
            target = engine.eval(where, state.alive);
        }

        int before = state.alive.cardinality();
        if (deleting) {
            state.alive.clear(target);
        } else {
            state.alive.merge(target);
        }
        int after = state.alive.cardinality();
        int changed = deleting ? (before - after) : (after - before);
        return new ChangeResult(changed, after, state.index.rowCount());
    }

    /** 索引空间与存活状态统计。 */
    public synchronized Map<String, Object> stats() {
        requireLoaded();
        BitmapIndex idx = state.index;
        int n = idx.rowCount();

        long indexRaw = idx.indexRawBytes();
        long indexRle = idx.indexCompressedBytes();
        long rawIndexWithMasks = indexRaw + 2L * state.alive.wordBytes();
        long rleMaskBytes = RleCodec.encode(state.alive).length;
        long rleIndexWithMasks = indexRle + 2L * rleMaskBytes;

        List<Map<String, Object>> cols = new ArrayList<>();
        for (String colName : idx.columns()) {
            BitmapIndex.ColumnIndex ci = idx.column(colName);
            List<Map<String, Object>> vals = new ArrayList<>();
            for (Map.Entry<String, Bitmap> e : ci.values.entrySet()) {
                byte[] coded = RleCodec.encode(e.getValue());
                Map<String, Object> v = new LinkedHashMap<>();
                v.put("value", e.getKey());
                v.put("count", e.getValue().cardinality());
                v.put("rawBytes", e.getValue().wordBytes());
                v.put("rleBytes", coded.length);
                vals.add(v);
            }
            Map<String, Object> cm = new LinkedHashMap<>();
            cm.put("name", colName);
            cm.put("distinctValues", ci.values.size());
            cm.put("rawBytes", ci.rawBytes);
            cm.put("rleBytes", ci.compressedBytes);
            cm.put("values", vals);
            cols.add(cm);
        }

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("source", state.source);
        out.put("loadedAt", new java.util.Date(state.loadedAtMillis).toInstant().toString());
        out.put("rowCount", n);
        out.put("aliveCount", state.alive.cardinality());
        out.put("deletedCount", n - state.alive.cardinality());
        out.put("aliveMaskRawBytes", state.alive.wordBytes());
        out.put("aliveMaskRleBytes", rleMaskBytes);
        out.put("dictionaryUtf8Bytes", idx.dictionaryBytes());
        out.put("rawDatasetBytes", state.rawDataBytes);
        out.put("indexRawBytes", indexRaw);
        out.put("indexRleBytes", indexRle);
        out.put("totalRawBytesInclMasks", rawIndexWithMasks + idx.dictionaryBytes());
        out.put("totalRleBytesInclMasks", rleIndexWithMasks + idx.dictionaryBytes());
        out.put("rleVsRawRatio",
                indexRaw == 0 ? null : Math.round(indexRle * 10000.0 / indexRaw) / 10000.0);
        out.put("columns", cols);
        return out;
    }

    private void requireLoaded() {
        if (state == null) {
            throw new IllegalStateException("尚未加载数据集，请先 POST /load");
        }
    }
}
