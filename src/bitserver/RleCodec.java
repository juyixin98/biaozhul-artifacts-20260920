package bitserver;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;

/**
 * 位图的无损游程编码（Run-Length Encoding）。
 *
 * 编码格式：
 *   byte0  firstBit —— 第一段游程的位值（0 或 1）
 *   随后为若干个无符号 LEB128 varint，依次表示各段游程的长度（位数，>= 1）。
 *
 * 因为只记录“位值 + 长度”，解码时严格按原始下标顺序还原，行 ID 不会因压缩
 * 而平移或重排。零位图与长度无关地只占 2 字节（firstBit + 长度），非常适合
 * 枚举列中大量稀疏的值位图；高基数列则会产生较多短游程，压缩率差，这一现象
 * 会真实反映在索引空间统计里。
 */
public final class RleCodec {

    private RleCodec() {
    }

    public static byte[] encode(Bitmap bm) {
        try {
            int n = bm.length();
            if (n == 0) {
                return new byte[0];
            }
            long[] words = bm.wordsArray();
            ByteArrayOutputStream out = new ByteArrayOutputStream();

            int cur = (int) (words[0] & 1L);
            out.write(cur);
            int runStart = 0; // 当前游程起始的全局位下标
            int pos = 0;      // 已处理到的全局位下标（始终等于实际行下标）

            for (int wi = 0; wi < words.length; wi++) {
                int base = wi << 6;
                int bits = Math.min(64, n - base);
                long w = words[wi];

                if (bits == 64 && (w == 0L || w == -1L)) {
                    int wv = w == 0L ? 0 : 1;
                    if (wv == cur) {
                        pos += 64; // 整个 word 属于当前游程
                    } else {
                        writeVarint(out, pos - runStart); // 在 word 边界处翻转
                        cur = wv;
                        runStart = pos;
                        pos += 64;
                    }
                    continue;
                }
                // 混合 word（或末尾非整 word，已被 Bitmap 尾部掩码清掉脏位）：逐位处理
                for (int b = 0; b < bits; b++) {
                    int v = (int) ((w >>> b) & 1L);
                    if (v != cur) {
                        writeVarint(out, pos - runStart);
                        cur = v;
                        runStart = pos;
                    }
                    pos++;
                }
            }
            writeVarint(out, pos - runStart);
            return out.toByteArray();
        } catch (IOException e) {
            throw new IllegalStateException("字节数组写入不应发生 IO 异常", e);
        }
    }

    public static Bitmap decode(byte[] data, int length) {
        Bitmap bm = new Bitmap(length);
        if (length == 0 || data.length == 0) {
            return bm;
        }
        try {
            ByteArrayInputStream in = new ByteArrayInputStream(data);
            int first = in.read();
            if (first < 0 || (first != 0 && first != 1)) {
                throw new IllegalArgumentException("RLE 数据非法：firstBit=" + first);
            }
            int bit = first;
            int pos = 0;
            while (pos < length) {
                int run = (int) readVarint(in);
                if (run < 1) {
                    throw new IllegalArgumentException("RLE 数据非法：游程长度为 0");
                }
                if (pos + run > length) {
                    throw new IllegalArgumentException("RLE 数据非法：游程超出位图长度");
                }
                if (bit == 1) {
                    bm.setRange(pos, pos + run);
                }
                pos += run;
                bit ^= 1;
            }
            if (in.read() != -1) {
                throw new IllegalArgumentException("RLE 数据非法：长度超出后仍有多余字节");
            }
            return bm;
        } catch (IOException e) {
            throw new IllegalArgumentException("RLE 数据被截断", e);
        }
    }

    static void writeVarint(ByteArrayOutputStream out, long value) throws IOException {
        if (value < 0) {
            throw new IllegalArgumentException("游程长度不能为负");
        }
        while ((value & ~0x7FL) != 0) {
            out.write((int) ((value & 0x7F) | 0x80));
            value >>>= 7;
        }
        out.write((int) value);
    }

    static long readVarint(ByteArrayInputStream in) throws IOException {
        long result = 0;
        int shift = 0;
        int b;
        do {
            b = in.read();
            if (b < 0) {
                throw new IOException("varint 被截断");
            }
            if (shift >= 63) {
                throw new IOException("varint 过长");
            }
            result |= (long) (b & 0x7F) << shift;
            shift += 7;
        } while ((b & 0x80) != 0);
        return result;
    }
}
