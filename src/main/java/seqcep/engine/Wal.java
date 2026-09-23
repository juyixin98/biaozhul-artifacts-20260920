package seqcep.engine;

import seqcep.json.Json;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.zip.CRC32;

/**
 * Append-only write-ahead log of every accepted event.
 *
 * <p>On-disk layout (big-endian):
 * <pre>
 *   magic: 8 bytes  "SEQCEPW1" + 0x0A
 *   record:
 *     uint64 payloadLength
 *     uint64 crc32(payload)
 *     bytes  payload  (UTF-8 JSON: {"seq","type","entity","ts"})
 * </pre>
 *
 * <p>Every append is {@link FileChannel#force(boolean) force(true)}'d before the engine
 * reports the event accepted, so after an acknowledged ingest (and a process crash at any
 * point afterwards) the record is recoverable.
 *
 * <p>Recovery is strict: an incomplete trailing record, a bad magic or a CRC mismatch raise
 * {@link WalCorruptionException}. The log is never truncated automatically — recovery of a
 * torn write caused by a crash mid-append is documented as an operator action in the README
 * (the unacknowledged write is discarded manually only with explicit intent).
 */
public final class Wal implements AutoCloseable {

    static final byte[] MAGIC = {'S', 'Q', 'C', 'E', 'P', 'W', '1', '\n'};
    static final int HEADER_BYTES = 16;

    private final Path file;
    private final FileChannel channel;

    private Wal(Path file, FileChannel channel) {
        this.file = file;
        this.channel = channel;
    }

    /** Opens or creates the log, writing/verifying the magic header. */
    public static synchronized Wal open(Path file) {
        try {
            Files.createDirectories(file.getParent());
            boolean fresh = !Files.exists(file) || Files.size(file) == 0;
            FileChannel ch = FileChannel.open(file,
                    StandardOpenOption.CREATE, StandardOpenOption.READ, StandardOpenOption.WRITE);
            if (fresh) {
                ch.write(ByteBuffer.wrap(MAGIC));
                ch.force(true);
            } else {
                ByteBuffer mb = ByteBuffer.allocate(MAGIC.length);
                readFully(ch, mb, 0);
                mb.flip();
                for (byte b : MAGIC) {
                    if (mb.get() != b) {
                        throw new WalCorruptionException("Bad WAL magic in " + file
                                + " — not a seqcep log or wrong format version");
                    }
                }
            }
            ch.position(ch.size());
            return new Wal(file, ch);
        } catch (IOException e) {
            throw new WalCorruptionException("Cannot open WAL " + file, e);
        }
    }

    /** Durably appends one event record. */
    public synchronized void append(Event e) {
        try {
            Map<String, Object> payload = Json.obj(
                    "seq", e.seq(),
                    "type", e.type(),
                    "entity", e.entityId(),
                    "ts", e.timestamp());
            byte[] body = Json.write(payload).getBytes(java.nio.charset.StandardCharsets.UTF_8);
            CRC32 crc = new CRC32();
            crc.update(body);

            ByteBuffer frame = ByteBuffer.allocate(HEADER_BYTES + body.length);
            frame.putLong(body.length);
            frame.putLong(crc.getValue());
            frame.put(body);
            frame.flip();

            while (frame.hasRemaining()) {
                channel.write(frame);
            }
            // Metadata too, so a rename/alloc of the file is also durable.
            channel.force(true);
        } catch (IOException ex) {
            throw new WalCorruptionException("Failed to append event seq=" + e.seq()
                    + " to WAL; engine must be restarted", ex);
        }
    }

    /** Reads every event record from a log file without mutating it. */
    public static List<Event> replay(Path file) {
        List<Event> events = new ArrayList<>();
        if (!Files.exists(file)) {
            return events;
        }
        try (FileChannel ch = FileChannel.open(file, StandardOpenOption.READ)) {
            long size = ch.size();
            if (size == 0) {
                return events;
            }
            ByteBuffer mb = ByteBuffer.allocate(MAGIC.length);
            readFully(ch, mb, 0);
            mb.flip();
            for (byte b : MAGIC) {
                if (mb.get() != b) {
                    throw new WalCorruptionException("Bad WAL magic in " + file);
                }
            }

            long pos = MAGIC.length;
            while (pos < size) {
                long remaining = size - pos;
                if (remaining < HEADER_BYTES) {
                    throw new WalCorruptionException(
                            "Truncated record header at byte " + pos + " in " + file
                                    + " (" + remaining + " trailing bytes); refusing to drop data");
                }
                ByteBuffer header = ByteBuffer.allocate(HEADER_BYTES);
                readFully(ch, header, pos);
                header.flip();
                long len = header.getLong();
                long expectedCrc = header.getLong();

                if (len < 0 || len > (16L * 1024 * 1024)) {
                    throw new WalCorruptionException(
                            "Implausible record length " + len + " at byte " + pos + " in " + file);
                }
                if (size - (pos + HEADER_BYTES) < len) {
                    throw new WalCorruptionException(
                            "Truncated record payload at byte " + pos + " in " + file
                                    + " (expected " + len + " bytes); refusing to drop data");
                }
                ByteBuffer body = ByteBuffer.allocate((int) len);
                readFully(ch, body, pos + HEADER_BYTES);
                body.flip();

                CRC32 crc = new CRC32();
                crc.update(body);
                if (crc.getValue() != expectedCrc) {
                    throw new WalCorruptionException(
                            "CRC mismatch on record at byte " + pos + " in " + file);
                }
                body.flip(); // crc.update() consumed the buffer; rewind before decoding

                String json = java.nio.charset.StandardCharsets.UTF_8.decode(body).toString();
                Map<String, Object> m = Json.parseObject(json);
                long seq = Json.requireLong(m, "seq");
                String type = Json.requireString(m, "type");
                String entity = Json.requireString(m, "entity");
                long ts = Json.requireLong(m, "ts");
                events.add(new Event(seq, type, entity, ts));
                pos += HEADER_BYTES + len;
            }
            return events;
        } catch (IOException e) {
            throw new WalCorruptionException("Cannot read WAL " + file, e);
        }
    }

    private static void readFully(FileChannel ch, ByteBuffer dst, long position) throws IOException {
        while (dst.hasRemaining()) {
            int n = ch.read(dst, position + dst.position());
            if (n == -1) {
                throw new WalCorruptionException("Unexpected EOF in " + ch + " at byte "
                        + (position + dst.position()));
            }
        }
    }

    public Path file() {
        return file;
    }

    @Override
    public synchronized void close() {
        try {
            channel.force(true);
            channel.close();
        } catch (IOException e) {
            throw new WalCorruptionException("Failed to close WAL " + file, e);
        }
    }
}
