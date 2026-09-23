package join;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * Binary run file format (little-endian-ish DataOutputStream, big-endian on disk):
 *
 *   repeated:
 *     long  key           (8 bytes, signed; NULL rows are never written)
 *     int   rawLength     (4 bytes, &gt;= 0)
 *     byte[rawLength] raw (original JSONL line, UTF-8)
 *
 * A length-prefixed binary form is used instead of JSONL so that keys do not have
 * to be re-parsed during the k-way merge and so that tabs/newlines inside JSON
 * string values cannot corrupt delimiters.
 */
public final class RunFiles {

    private RunFiles() {
    }

    public static Writer writer(Path path) throws IOException {
        return new Writer(new DataOutputStream(
                new BufferedOutputStream(Files.newOutputStream(path))), path);
    }

    public static Reader reader(Path path) throws IOException {
        return new Reader(new DataInputStream(
                new BufferedInputStream(Files.newInputStream(path), 1 << 16)), path);
    }

    public static final class Writer implements AutoCloseable {
        private final DataOutputStream out;
        private final Path path;
        private long bytes;
        private long rows;
        private boolean closed;

        Writer(DataOutputStream out, Path path) {
            this.out = out;
            this.path = path;
        }

        public void write(Record r) throws IOException {
            if (r.key == null) {
                throw new IllegalArgumentException("NULL records must not be spilled");
            }
            out.writeLong(r.key);
            out.writeInt(r.raw.length);
            out.write(r.raw);
            rows++;
        }

        public long bytesWritten() {
            return bytes;
        }

        public long rowsWritten() {
            return rows;
        }

        public Path path() {
            return path;
        }

        @Override
        public void close() throws IOException {
            if (closed) {
                return;
            }
            closed = true;
            out.flush();
            bytes = Files.size(path);
            out.close();
        }
    }

    public static final class Reader implements AutoCloseable {
        private final DataInputStream in;
        private final Path path;
        private Record head;
        private boolean atEof;

        Reader(DataInputStream in, Path path) throws IOException {
            this.in = in;
            this.path = path;
            advance();
        }

        private void advance() throws IOException {
            long key;
            try {
                key = in.readLong();
            } catch (EOFException e) {
                head = null;
                atEof = true;
                return;
            }
            int len = in.readInt();
            if (len < 0) {
                throw new IOException("Corrupt run file " + path + ": negative length");
            }
            byte[] raw = new byte[len];
            in.readFully(raw);
            head = new Record(key, raw);
        }

        public Record peek() {
            return head;
        }

        public Record next() throws IOException {
            Record r = head;
            if (!atEof) {
                advance();
            }
            return r;
        }

        public boolean atEof() {
            return atEof;
        }

        @Override
        public void close() throws IOException {
            in.close();
        }
    }

    /** Delete a file, never throwing (used in cleanup paths). */
    public static void deleteQuietly(Path p) {
        if (p == null) {
            return;
        }
        try {
            Files.deleteIfExists(p);
        } catch (IOException | RuntimeException ignored) {
            // best-effort cleanup
        }
    }
}
