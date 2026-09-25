package invidx.deletes;

import invidx.model.DocKey;
import invidx.store.Directory;
import invidx.util.Codecs;

import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Append-only kill log ({@code deletes.log}).
 *
 * <p>One kill of a published revision is a <b>single</b> record:
 * <pre>
 *   'K' vlong(id) vlong(killedGen) vlong(nextGen) sha256hex(whole body) '\n'
 * </pre>
 * Tombstoning the old generation and advancing the id's generation counter
 * therefore cannot partially survive: a torn final write fails the record
 * checksum and is truncated on open, so either both effects replay or
 * neither does. The latter case is also safe — the caller only returns
 * success after the fsync of this write.
 *
 * <p>{@link #open()} replays the committed prefix and repairs any torn
 * tail by truncation.
 */
public final class DeleteLog {

    private static final byte[] HEADER = "IVDEL003\n".getBytes(StandardCharsets.US_ASCII);
    private static final byte TAG_KILL = 'K';

    /** Result of replaying the log. */
    public record Replay(List<Long> tombstones, Map<Integer, Long> genAdvances) {
    }

    private final Directory dir;
    private final Path file;

    public DeleteLog(Directory dir) {
        this.dir = dir;
        this.file = dir.resolve("deletes.log");
    }

    public synchronized Replay open() throws java.io.IOException {
        if (!dir.exists(file)) {
            dir.writeNewDurable(file, HEADER);
            return new Replay(List.of(), Map.of());
        }
        byte[] data = dir.readAll(file);
        if (data.length < HEADER.length || !matches(data, 0, HEADER)) {
            throw new IllegalStateException("deletes.log has a bad header");
        }
        int pos = HEADER.length;

        List<Long> tombstones = new ArrayList<>();
        Map<Integer, Long> genAdvances = new HashMap<>();
        while (pos < data.length) {
            int recordStart = pos;
            try {
                byte tag = data[pos];
                if (tag != TAG_KILL) {
                    throw new Codecs.CorruptFormatException("unknown tag " + (char) tag);
                }
                Codecs.Reader r = new Codecs.Reader(data, pos + 1, data.length);
                int id = (int) r.readVLong();
                long killedGen = r.readVLong();
                long nextGen = r.readVLong();
                int bodyEnd = r.position();

                if (bodyEnd + 65 > data.length || data[bodyEnd + 64] != '\n') {
                    throw new Codecs.CorruptFormatException("truncated record");
                }
                String given = new String(data, bodyEnd, 64, StandardCharsets.US_ASCII);
                String actual = Directory.sha256(
                        java.util.Arrays.copyOfRange(data, recordStart, bodyEnd));
                if (!given.equalsIgnoreCase(actual)) {
                    throw new Codecs.CorruptFormatException("checksum mismatch");
                }
                tombstones.add(DocKey.encode(id, killedGen));
                genAdvances.merge(id, nextGen, Math::max);
                pos = bodyEnd + 65;
            } catch (RuntimeException bad) {
                truncateTo(recordStart);
                break;
            }
        }
        return new Replay(tombstones, genAdvances);
    }

    /**
     * Durably record one kill: tombstone for {@code killedGen} and the
     * resulting next generation for the id, as one atomic record.
     * Caller holds the index write lock.
     */
    public synchronized void appendKill(int id, long killedGen, long nextGen)
            throws java.io.IOException {
        int bodyLen = 1 + Codecs.vLongSize(id & 0xFFFFFFFFL)
                + Codecs.vLongSize(killedGen) + Codecs.vLongSize(nextGen);
        byte[] body = new byte[bodyLen];
        body[0] = TAG_KILL;
        int p = Codecs.writeVLong(body, 1, id & 0xFFFFFFFFL);
        p = Codecs.writeVLong(body, p, killedGen);
        Codecs.writeVLong(body, p, nextGen);

        String hex = Directory.sha256(body);
        byte[] record = new byte[bodyLen + 65];
        System.arraycopy(body, 0, record, 0, bodyLen);
        byte[] hexBytes = hex.getBytes(StandardCharsets.US_ASCII);
        System.arraycopy(hexBytes, 0, record, bodyLen, 64);
        record[record.length - 1] = '\n';
        dir.appendDurable(file, record);
    }

    private void truncateTo(int position) throws java.io.IOException {
        try (FileChannel ch = FileChannel.open(file, StandardOpenOption.WRITE)) {
            ch.truncate(position);
            ch.force(true);
        }
        dir.fsyncDir(file.getParent());
    }

    private static boolean matches(byte[] data, int at, byte[] expected) {
        if (at + expected.length > data.length) {
            return false;
        }
        for (int i = 0; i < expected.length; i++) {
            if (data[at + i] != expected[i]) {
                return false;
            }
        }
        return true;
    }
}
