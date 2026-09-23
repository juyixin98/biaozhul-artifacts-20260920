package colscan;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.channels.ReadableByteChannel;
import java.nio.channels.SeekableByteChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Columnar shard file (little-endian) and on-disk scanning with byte accounting.
 *
 * <pre>
 * offset 0  : magic "CSHF" (4 bytes), version uint16
 * then, per column in footer-declared order:
 *   present-bit bytes : ceil(rows/8), row r present iff bit (r &amp; 7) of byte (r &gt;&gt; 3) is 1
 *   values            : rows * 8 bytes, int64 little-endian (uninitialized for NULL rows)
 * footer    : UTF-8 JSON
 * footerLen : int32 little-endian (length of footer JSON)
 * tail      : magic "CSFT"
 * </pre>
 *
 * Bytes are counted from the underlying channel: the scan only physically reads
 * (a) the footer (to obtain statistics) and (b) chunks of columns actually
 * referenced by the query. Values are read row-by-row, so skipped rows cost
 * nothing beyond the present-bit bytes. {@link #bytesRead()} therefore reports
 * genuine disk read traffic, a proxy for a real column-store's IO.
 */
public final class ColumnFile {

    static final byte[] MAGIC_HEAD = "CSHF".getBytes(StandardCharsets.US_ASCII);
    static final byte[] MAGIC_TAIL = "CSFT".getBytes(StandardCharsets.US_ASCII);
    static final int VERSION = 1;

    private ColumnFile() {
    }

    static void putU16(ByteBuffer b, int v) {
        b.put((byte) (v & 0xFF));
        b.put((byte) ((v >>> 8) & 0xFF));
    }

    static int getU16(ByteBuffer b) {
        return (b.get() & 0xFF) | ((b.get() & 0xFF) << 8);
    }

    static void putU32(ByteBuffer b, long v) {
        b.put((byte) (v & 0xFF));
        b.put((byte) ((v >>> 8) & 0xFF));
        b.put((byte) ((v >>> 16) & 0xFF));
        b.put((byte) ((v >>> 24) & 0xFF));
    }

    static long getU32(ByteBuffer b) {
        return (b.get() & 0xFFL)
                | ((b.get() & 0xFFL) << 8)
                | ((b.get() & 0xFFL) << 16)
                | ((b.get() & 0xFFL) << 24);
    }

    /** Persist a shard to {@code dataDir/table__shardId.csc}. */
    public static Path write(Path dataDir, Shard shard) throws IOException {
        Files.createDirectories(dataDir);
        String file = shard.table.replace('/', '_') + "__" + shard.shardId.replace('/', '_') + ".csc";
        Path path = dataDir.resolve(file);

        List<String> names = shard.columnNames();
        StringBuilder footer = new StringBuilder();
        footer.append("{\"table\":").append(Json.quote(shard.table));
        footer.append(",\"shardId\":").append(Json.quote(shard.shardId));
        footer.append(",\"rowCount\":").append(shard.rowCount);
        footer.append(",\"columns\":[");
        for (int i = 0; i < names.size(); i++) {
            if (i > 0) footer.append(',');
            footer.append(Json.quote(names.get(i)));
        }
        footer.append("],\"stats\":{");
        for (int i = 0; i < names.size(); i++) {
            if (i > 0) footer.append(',');
            Shard.Stats s = shard.stats(names.get(i));
            footer.append(Json.quote(names.get(i))).append(':');
            footer.append('{');
            footer.append("\"min\":").append(s.min == null ? "null" : s.min);
            footer.append(",\"max\":").append(s.max == null ? "null" : s.max);
            footer.append(",\"nullCount\":").append(s.nullCount == null ? "null" : s.nullCount);
            footer.append('}');
        }
        footer.append("}}");
        byte[] footerBytes = footer.toString().getBytes(StandardCharsets.UTF_8);

        int presentBytes = (shard.rowCount + 7) / 8;
        long bodySize = 0;
        for (String name : names) {
            bodySize += presentBytes + (long) shard.rowCount * 8;
        }
        long total = 6 + bodySize + footerBytes.length + 8L;

        ByteBuffer buf = ByteBuffer.allocate((int) total).order(ByteOrder.LITTLE_ENDIAN);
        buf.put(MAGIC_HEAD);
        putU16(buf, VERSION);
        for (String name : names) {
            long[] data = shard.data(name);
            boolean[] pres = shard.presence(name);
            byte[] bits = new byte[presentBytes];
            for (int r = 0; r < shard.rowCount; r++) {
                if (pres[r]) {
                    bits[r >>> 3] |= (byte) (1 << (r & 7));
                }
            }
            buf.put(bits);
            for (int r = 0; r < shard.rowCount; r++) {
                buf.putLong(data[r]);
            }
        }
        buf.put(footerBytes);
        putU32(buf, footerBytes.length);
        buf.put(MAGIC_TAIL);
        if (buf.position() != total) {
            throw new IOException("internal: wrote " + buf.position() + " expected " + total);
        }
        buf.flip();
        try (var ch = Files.newByteChannel(path, StandardOpenOption.CREATE,
                StandardOpenOption.WRITE, StandardOpenOption.TRUNCATE_EXISTING)) {
            while (buf.hasRemaining()) {
                ch.write(buf);
            }
        }
        return path;
    }

    /** Counts every byte physically pulled through the channel. */
    private static final class CountingChannel implements ReadableByteChannel {
        final SeekableByteChannel delegate;
        long bytesRead;
        boolean open = true;

        CountingChannel(SeekableByteChannel delegate) {
            this.delegate = delegate;
        }

        @Override
        public int read(ByteBuffer dst) throws IOException {
            int n = delegate.read(dst);
            if (n > 0) bytesRead += n;
            return n;
        }

        long position() throws IOException {
            return delegate.position();
        }

        void position(long p) throws IOException {
            delegate.position(p);
        }

        @Override public boolean isOpen() {
            return open;
        }

        @Override public void close() throws IOException {
            open = false;
            delegate.close();
        }
    }

    private static void readFully(ReadableByteChannel ch, ByteBuffer b) throws IOException {
        while (b.hasRemaining()) {
            int n = ch.read(b);
            if (n < 0) throw new IOException("unexpected EOF");
        }
    }

    /** Handle to a persisted shard: statistics come from the footer, data is read on demand. */
    public static final class Handle {
        public final Path path;
        public final String table;
        public final String shardId;
        public final int rowCount;
        public final List<String> columns;
        public final Map<String, Shard.Stats> stats = new LinkedHashMap<>();

        private Handle(Path path, String table, String shardId, int rowCount, List<String> columns) {
            this.path = path;
            this.table = table;
            this.shardId = shardId;
            this.rowCount = rowCount;
            this.columns = columns;
        }
    }

    /**
     * Read just the footer (magic + JSON + length + magic) and return a handle.
     * Bytes physically read are charged to the returned counter holder via the
     * scan path; this method on its own is also used by metadata endpoints.
     */
    public static Handle open(Path path) throws IOException {
        try (SeekableByteChannel raw = Files.newByteChannel(path, StandardOpenOption.READ)) {
            long size = raw.size();
            if (size < 14) {
                throw new IOException("file too short: " + path);
            }
            ByteBuffer tail = ByteBuffer.allocate(8);
            raw.position(size - 8);
            readFully(raw, tail);
            tail.flip();
            long footerLen = getU32(tail);
            byte[] magic = new byte[4];
            tail.get(magic);
            if (!new String(magic, StandardCharsets.US_ASCII).equals("CSFT")) {
                throw new IOException("bad tail magic in " + path);
            }
            if (footerLen > size - 14) {
                throw new IOException("footer length out of range in " + path);
            }
            ByteBuffer fb = ByteBuffer.allocate((int) footerLen);
            raw.position(size - 8 - footerLen);
            readFully(raw, fb);
            fb.flip();
            String json = StandardCharsets.UTF_8.decode(fb).toString();
            return parseFooter(path, json);
        }
    }

    @SuppressWarnings("unchecked")
    private static Handle parseFooter(Path path, String json) throws IOException {
        Object f;
        try {
            f = Json.parse(json);
        } catch (Exception e) {
            throw new IOException("corrupt footer in " + path + ": " + e.getMessage());
        }
        if (!(f instanceof Map)) {
            throw new IOException("corrupt footer in " + path);
        }
        Map<String, Object> m = (Map<String, Object>) f;
        String table = (String) m.get("table");
        String shardId = (String) m.get("shardId");
        Object rc = m.get("rowCount");
        if (!(rc instanceof Number) || table == null || shardId == null) {
            throw new IOException("corrupt footer in " + path);
        }
        Handle h = new Handle(path, table, shardId, ((Number) rc).intValue(),
                Json.toStringList(m.get("columns")));
        Object st = m.get("stats");
        if (!(st instanceof Map)) {
            throw new IOException("corrupt footer stats in " + path);
        }
        for (Map.Entry<String, Object> e : ((Map<String, Object>) st).entrySet()) {
            if (!(e.getValue() instanceof Map)) {
                throw new IOException("corrupt column stats in " + path);
            }
            Map<String, Object> cs = (Map<String, Object>) e.getValue();
            h.stats.put(e.getKey(), new Shard.Stats(
                    Json.toLong(cs.get("min")),
                    Json.toLong(cs.get("max")),
                    Json.toLong(cs.get("nullCount"))));
        }
        return h;
    }

    /** A live scanning session over one shard file; tracks bytes read. */
    public static final class Scanner implements AutoCloseable {
        private final CountingChannel ch;
        private final Handle handle;
        private long columnStart;
        private int presentBytes;
        private long metadataBytes;
        private final Map<String, Integer> columnIndex = new LinkedHashMap<>();

        private Scanner(Handle handle) throws IOException {
            this.handle = handle;
            this.ch = new CountingChannel(Files.newByteChannel(handle.path, StandardOpenOption.READ));
            // Head: magic + version, always read.
            ByteBuffer head = ByteBuffer.allocate(6);
            readFully(ch, head);
            head.flip();
            byte[] m = new byte[4];
            head.get(m);
            if (!new String(m, StandardCharsets.US_ASCII).equals("CSHF")) {
                throw new IOException("bad head magic in " + handle.path);
            }
            if (getU16(head) != VERSION) {
                throw new IOException("unsupported version in " + handle.path);
            }
            columnStart = ch.position();
            presentBytes = (handle.rowCount + 7) / 8;
            for (int i = 0; i < handle.columns.size(); i++) {
                columnIndex.put(handle.columns.get(i), i);
            }
        }

        private long chunkStart(String col) {
            Integer idx = columnIndex.get(col);
            if (idx == null) {
                throw new IllegalArgumentException("no such column in shard: " + col);
            }
            long stride = presentBytes + (long) handle.rowCount * 8;
            return columnStart + idx * stride;
        }

        /** Read the footer for statistics and charge those bytes as metadata IO. */
        void loadMetadata() throws IOException {
            long before = ch.bytesRead;
            long size = ch.delegate.size();
            ByteBuffer tail = ByteBuffer.allocate(8);
            ch.position(size - 8);
            readFully(ch, tail);
            tail.flip();
            long footerLen = getU32(tail);
            tail.get(new byte[4]); // tail magic, validated at write/open time
            ByteBuffer fb = ByteBuffer.allocate((int) footerLen);
            ch.position(size - 8 - footerLen);
            readFully(ch, fb);
            metadataBytes = ch.bytesRead - before;
        }

        long metadataBytes() {
            return metadataBytes;
        }

        /**
         * Open one column chunk for scanning. Present bits are read immediately;
         * values are pulled one int64 at a time, so pruning/skipping rows saves IO.
         */
        ColumnScanner scanColumn(String col) throws IOException {
            long start = chunkStart(col);
            ch.position(start);
            byte[] bits = new byte[presentBytes];
            readFully(ch, ByteBuffer.wrap(bits));
            return new ColumnScanner(ch, start + presentBytes, bits);
        }

        long bytesRead() {
            return ch.bytesRead;
        }

        @Override public void close() throws IOException {
            ch.close();
        }
    }

    /** Per-column cursor over a shard file. */
    public static final class ColumnScanner {
        private final CountingChannel ch;
        private final long valuesOffset;
        private final byte[] presentBits;
        private final ByteBuffer oneLong = ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN);

        ColumnScanner(CountingChannel ch, long valuesOffset, byte[] presentBits) {
            this.ch = ch;
            this.valuesOffset = valuesOffset;
            this.presentBits = presentBits;
        }

        public boolean isNull(int row) {
            return ((presentBits[row >>> 3] >> (row & 7)) & 1) == 0;
        }

        public long getLong(int row) throws IOException {
            ch.position(valuesOffset + (long) row * 8);
            oneLong.clear();
            readFully(ch, oneLong);
            oneLong.flip();
            return oneLong.getLong();
        }
    }

    /** Start a fresh scanning session (header validated lazily on first use). */
    public static Scanner newScanner(Handle handle) throws IOException {
        return new Scanner(handle);
    }
}
