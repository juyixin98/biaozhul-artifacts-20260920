package colscan.store;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * 二进制列文件格式（大端序）：
 * <pre>
 * magic   7 字节  ASCII "CLCOL1"
 * type    1 字节  1 = LONG, 2 = DOUBLE
 * rows    4 字节  int32，该分片的总行数（含 NULL）
 * nulls   4 字节  int32，NULL 行数
 * nullbits ceil(rows/8) 字节  每位 1 表示该行“非 NULL”（全 NULL 分片此处全 0）
 * values  (rows - nulls) 个定宽值  LONG=int64，DOUBLE=float64
 * </pre>
 * 列式：每个列独立一个文件，查询只读被引用的列文件。
 */
public final class ColumnFile {

    static final byte[] MAGIC = {'C', 'L', 'C', 'O', 'L', '1', 0};
    static final byte TYPE_LONG = 1;
    static final byte TYPE_DOUBLE = 2;

    /** 一次列扫描的结果 + 字节账。 */
    public static final class ReadResult {
        public final Object[] values;
        public final long bytesRead;

        ReadResult(Object[] values, long bytesRead) {
            this.values = values;
            this.bytesRead = bytesRead;
        }
    }

    private ColumnFile() {}

    public static void write(Path file, String type, Object[] values) throws IOException {
        byte typeCode = type.equals(Types.LONG) ? TYPE_LONG : TYPE_DOUBLE;
        int nullCount = 0;
        for (Object v : values) {
            if (v == null) nullCount++;
        }
        Files.createDirectories(file.getParent());
        try (OutputStream raw = Files.newOutputStream(file);
             BufferedOutputStream buf = new BufferedOutputStream(raw);
             DataOutputStream out = new DataOutputStream(buf)) {
            out.write(MAGIC);
            out.writeByte(typeCode);
            out.writeInt(values.length);
            out.writeInt(nullCount);

            byte[] bits = new byte[(values.length + 7) / 8];
            int valueIndex = 0;
            for (int i = 0; i < values.length; i++) {
                if (values[i] != null) {
                    bits[i >> 3] |= (byte) (1 << (i & 7));
                    valueIndex++;
                }
            }
            out.write(bits);

            for (Object v : values) {
                if (v == null) continue;
                if (typeCode == TYPE_LONG) {
                    out.writeLong(((Number) v).longValue());
                } else {
                    out.writeDouble(((Number) v).doubleValue());
                }
            }
        }
    }

    /** 读取整列。返回的值数组里 NULL 用 Java null 表示（绝不置零）。 */
    public static ReadResult read(Path file) throws IOException {
        try (InputStream raw = Files.newInputStream(file);
             BufferedInputStream buf = new BufferedInputStream(raw);
             CountingInputStream counted = new CountingInputStream(buf);
             DataInputStream in = new DataInputStream(counted)) {
            byte[] magic = new byte[MAGIC.length];
            in.readFully(magic);
            for (int i = 0; i < MAGIC.length; i++) {
                if (magic[i] != MAGIC[i]) {
                    throw new IOException("bad column file magic: " + file);
                }
            }
            byte typeCode = in.readByte();
            if (typeCode != TYPE_LONG && typeCode != TYPE_DOUBLE) {
                throw new IOException("unknown column type code in " + file);
            }
            int rows = in.readInt();
            if (rows < 0) throw new IOException("negative row count in " + file);
            int nullCount = in.readInt();
            if (nullCount < 0 || nullCount > rows) {
                throw new IOException("invalid null count in " + file);
            }

            byte[] bits = new byte[(rows + 7) / 8];
            in.readFully(bits);

            int nonNull = rows - nullCount;
            long[] longs = typeCode == TYPE_LONG ? new long[nonNull] : null;
            double[] doubles = typeCode == TYPE_DOUBLE ? new double[nonNull] : null;
            try {
                if (typeCode == TYPE_LONG) {
                    for (int i = 0; i < nonNull; i++) longs[i] = in.readLong();
                } else {
                    for (int i = 0; i < nonNull; i++) doubles[i] = in.readDouble();
                }
            } catch (EOFException eof) {
                throw new IOException("truncated column file: " + file, eof);
            }

            Object[] values = new Object[rows];
            int valueIndex = 0;
            for (int i = 0; i < rows; i++) {
                boolean present = (bits[i >> 3] & (1 << (i & 7))) != 0;
                if (present) {
                    // 注意：不能用三目 (cond ? long : double)，数字条件表达式会把 long 提升为 double
                    if (typeCode == TYPE_LONG) {
                        values[i] = longs[valueIndex];
                    } else {
                        values[i] = doubles[valueIndex];
                    }
                    valueIndex++;
                }
                // 缺省即为 null —— 不会被当成 0
            }
            if (valueIndex != nonNull) {
                throw new IOException("null bitmap/data mismatch in " + file);
            }
            return new ReadResult(values, counted.bytesRead());
        }
    }
}
