package invidx.util;

import java.nio.charset.StandardCharsets;

/**
 * LEB128 variable-length integer codecs (little-endian base 128).
 * Non-negative integers only; generation always fits in 32 bits.
 */
public final class Codecs {

    private Codecs() {
    }

    public static int writeVLong(byte[] buf, int pos, long value) {
        if (value < 0) {
            throw new IllegalArgumentException("negative vlong: " + value);
        }
        while (value >= 0x80L) {
            buf[pos++] = (byte) (value | 0x80L);
            value >>>= 7;
        }
        buf[pos++] = (byte) value;
        return pos;
    }

    public static int vLongSize(long value) {
        int n = 1;
        while (value >= 0x80L) {
            value >>>= 7;
            n++;
        }
        return n;
    }

    /** Streaming reader over a byte array with a moving position. */
    public static final class Reader {
        private final byte[] buf;
        private int pos;
        private final int end;

        public Reader(byte[] buf) {
            this(buf, 0, buf.length);
        }

        public Reader(byte[] buf, int pos, int end) {
            this.buf = buf;
            this.pos = pos;
            this.end = end;
        }

        public int position() {
            return pos;
        }

        public boolean hasMore() {
            return pos < end;
        }

        public long readVLong() {
            long result = 0;
            int shift = 0;
            while (true) {
                if (pos >= end) {
                    throw new CorruptFormatException("truncated vlong");
                }
                byte b = buf[pos++];
                result |= (long) (b & 0x7F) << shift;
                if ((b & 0x80) == 0) {
                    return result;
                }
                shift += 7;
                if (shift > 63) {
                    throw new CorruptFormatException("vlong too long");
                }
            }
        }

        public int readVInt() {
            long v = readVLong();
            if (v > Integer.MAX_VALUE) {
                throw new CorruptFormatException("vint overflow");
            }
            return (int) v;
        }

        public byte[] readBytes(int n) {
            if (n < 0 || pos + n > end) {
                throw new CorruptFormatException("truncated byte run");
            }
            byte[] out = new byte[n];
            System.arraycopy(buf, pos, out, 0, n);
            pos += n;
            return out;
        }

        public String readString(int n) {
            return new String(readBytes(n), StandardCharsets.UTF_8);
        }
    }

    public static class CorruptFormatException extends RuntimeException {
        public CorruptFormatException(String message) {
            super(message);
        }
    }
}
