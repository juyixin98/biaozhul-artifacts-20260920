package bitserver;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 离线数据集的多维位图索引（不可变）。
 *
 * 每个枚举列维护一个“值 -> 位图”字典；位 i 为 1 表示行 ID i 在该列取该值。
 * 行 ID 等于数据加载时的行下标（0 基，CSV 表头不算行），索引一旦建立，
 * 行 ID 永不变化；删除由上层的存活掩码处理，不改动本索引。
 *
 * 空间统计同时给出未压缩字节数（long[] 位图）与 RLE 压缩字节数，
 * 压缩/解压经 {@link RleCodec} 双向验证后才计入统计，保证统计数据可复现。
 */
public final class BitmapIndex {

    public static final class ColumnIndex {
        public final String name;
        /** 值字典，插入顺序保持稳定（便于统计输出可读）。 */
        public final Map<String, Bitmap> values = new LinkedHashMap<>();
        /** 未压缩字节（各值位图的 long[] 字节之和）。 */
        public int rawBytes;
        /** RLE 编码字节之和。 */
        public int compressedBytes;

        ColumnIndex(String name) {
            this.name = name;
        }
    }

    private final List<String> columns;
    private final int rowCount;
    private final Map<String, ColumnIndex> columnIndexMap = new LinkedHashMap<>();
    private final long dictionaryBytes;

    private BitmapIndex(List<String> columns, int rowCount,
                        Map<String, ColumnIndex> columnIndexMap, long dictionaryBytes) {
        this.columns = columns;
        this.rowCount = rowCount;
        this.dictionaryBytes = dictionaryBytes;
        for (Map.Entry<String, ColumnIndex> e : columnIndexMap.entrySet()) {
            this.columnIndexMap.put(e.getKey(), e.getValue());
        }
    }

    /**
     * 从行数据构建索引。
     *
     * @param columns 列名（决定列序）
     * @param rows    行数据，每行列数与 columns 一致；行下标即行 ID
     */
    public static BitmapIndex build(List<String> columns, List<List<String>> rows) {
        if (columns == null || columns.isEmpty()) {
            throw new IllegalArgumentException("至少需要一列");
        }
        int n = rows.size();
        Map<String, ColumnIndex> idx = new LinkedHashMap<>();
        for (String col : columns) {
            idx.put(col, new ColumnIndex(col));
        }

        for (int r = 0; r < n; r++) {
            List<String> row = rows.get(r);
            if (row.size() != columns.size()) {
                throw new IllegalArgumentException(
                        "第 " + r + " 行有 " + row.size() + " 列，与列数 " + columns.size() + " 不一致");
            }
            for (int c = 0; c < columns.size(); c++) {
                String value = row.get(c);
                ColumnIndex ci = idx.get(columns.get(c));
                Bitmap bm = ci.values.get(value);
                if (bm == null) {
                    bm = new Bitmap(Math.max(n, 1));
                    ci.values.put(value, bm);
                }
                bm.set(r);
            }
        }

        long dictBytes = 0;
        for (ColumnIndex ci : idx.values()) {
            ci.rawBytes = 0;
            ci.compressedBytes = 0;
            for (Map.Entry<String, Bitmap> e : ci.values.entrySet()) {
                Bitmap bm = e.getValue();
                ci.rawBytes += bm.wordBytes();
                byte[] coded = RleCodec.encode(bm);
                // 立即做一次往返校验：压缩必须能按原始行 ID 无损还原
                Bitmap decoded = RleCodec.decode(coded, n);
                for (int r = 0; r < n; r++) {
                    if (decoded.get(r) != bm.get(r)) {
                        throw new IllegalStateException("RLE 往返校验失败: " + ci.name + "=" + e.getKey() + " 行 " + r);
                    }
                }
                ci.compressedBytes += coded.length;
                dictBytes += stringBytes(ci.name) + stringBytes(e.getKey());
            }
        }
        return new BitmapIndex(columns, n, idx, dictBytes);
    }

    private static long stringBytes(String s) {
        // 字典字符串按 UTF-8 估算（JDK 内部存储可能更省，但统计口径用标准编码）
        return s.getBytes(java.nio.charset.StandardCharsets.UTF_8).length;
    }

    public List<String> columns() {
        return columns;
    }

    public int rowCount() {
        return rowCount;
    }

    public ColumnIndex column(String name) {
        return columnIndexMap.get(name);
    }

    /** 返回某列某值的位图（不可为 null —— 不存在的值语义上是空位图）。 */
    public Bitmap bitmapFor(String column, String value) {
        ColumnIndex ci = columnIndexMap.get(column);
        if (ci == null) {
            throw new IllegalArgumentException("未知列: " + column);
        }
        Bitmap bm = ci.values.get(value);
        return bm == null ? new Bitmap(rowCount) : bm;
    }

    public long dictionaryBytes() {
        return dictionaryBytes;
    }

    public int indexRawBytes() {
        int total = 0;
        for (ColumnIndex ci : columnIndexMap.values()) {
            total += ci.rawBytes;
        }
        return total;
    }

    public int indexCompressedBytes() {
        int total = 0;
        for (ColumnIndex ci : columnIndexMap.values()) {
            total += ci.compressedBytes;
        }
        return total;
    }

    public Map<String, ColumnIndex> columnIndexes() {
        return columnIndexMap;
    }
}
