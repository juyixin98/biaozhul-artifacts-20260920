package dev.dedup.hll;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.zip.CRC32;

/**
 * HLL 草图的紧凑二进制序列化格式（v1）。
 *
 * <pre>
 * 偏移   长度  字段
 * 0      4    魔数 0x48 4C 43 44 ("HLCD")
 * 4      1    格式版本，当前 = 1
 * 5      1    精度 p (4..18)
 * 6      1    哈希标识长度 L (1..255)
 * 7      1    预留标志位，v1 必须为 0
 * 8      4    CRC32（对偏移 12 之后的全部字节计算，大端无符号）
 * 12     L    哈希标识 UTF-8 字节
 * 12+L   m    寄存器字节，m = 2^p，每个值范围 0..(65-p)
 * </pre>
 *
 * 所有多字节整数为大端序。反序列化对魔数、版本、长度、寄存器值域、
 * 标志位和 CRC 全部做严格校验，任何不符抛出 {@link SketchFormatException}。
 */
public final class BinarySketchCodec {

    public static final int VERSION = 1;
    public static final byte[] MAGIC = {'H', 'L', 'C', 'D'};
    public static final int HEADER_LEN = 12;

    private BinarySketchCodec() {
    }

    public static byte[] encode(HllSketch sketch) {
        byte[] hashBytes = sketch.config().hashId().getBytes(StandardCharsets.UTF_8);
        if (hashBytes.length > 255) {
            throw new SketchFormatException("哈希标识超过 255 字节: " + hashBytes.length);
        }
        byte[] regs = sketch.registersSnapshot();
        ByteArrayOutputStream out = new ByteArrayOutputStream(HEADER_LEN + hashBytes.length + regs.length);
        out.write(MAGIC, 0, 4);
        out.write(VERSION);
        out.write(sketch.config().precision());
        out.write(hashBytes.length);
        out.write(0); // flags
        long crc = crc(hashBytes, regs);
        out.write((int) (crc >>> 24) & 0xff);
        out.write((int) (crc >>> 16) & 0xff);
        out.write((int) (crc >>> 8) & 0xff);
        out.write((int) crc & 0xff);
        out.write(hashBytes, 0, hashBytes.length);
        out.write(regs, 0, regs.length);
        return out.toByteArray();
    }

    public static HllSketch decode(byte[] data) {
        if (data == null) {
            throw new SketchFormatException("数据为 null");
        }
        if (data.length < HEADER_LEN) {
            throw new SketchFormatException("长度不足头部: " + data.length + " < " + HEADER_LEN);
        }
        for (int i = 0; i < 4; i++) {
            if (data[i] != MAGIC[i]) {
                throw new SketchFormatException("魔数错误（不是 HLCD 草图）");
            }
        }
        int version = data[4] & 0xff;
        if (version != VERSION) {
            throw new SketchFormatException("不支持的格式版本: " + version + "（仅支持 v" + VERSION + "）");
        }
        int p = data[5] & 0xff;
        if (p < HllConfig.MIN_PRECISION || p > HllConfig.MAX_PRECISION) {
            throw new SketchFormatException(
                    "精度越界: p=" + p + "，允许 [" + HllConfig.MIN_PRECISION + "," + HllConfig.MAX_PRECISION + "]");
        }
        int hashLen = data[6] & 0xff;
        if (hashLen == 0) {
            throw new SketchFormatException("哈希标识长度为 0");
        }
        int flags = data[7] & 0xff;
        if (flags != 0) {
            throw new SketchFormatException("存在 v1 无法识别的标志位: 0x" + Integer.toHexString(flags));
        }
        int m = 1 << p;
        int expectedLen = HEADER_LEN + hashLen + m;
        if (data.length != expectedLen) {
            throw new SketchFormatException(
                    "总长度与声明不符: 实际 " + data.length + "，应为 " + expectedLen
                            + "（头部12 + hashId " + hashLen + " + 寄存器 " + m + "）");
        }
        long crcExpected = ByteBuffer.wrap(data, 8, 4).order(ByteOrder.BIG_ENDIAN).getInt() & 0xffffffffL;
        long crcActual = crcRange(data, HEADER_LEN, hashLen + m);
        if (crcExpected != crcActual) {
            throw new SketchFormatException(
                    "CRC32 校验失败: 期望 " + Long.toHexString(crcExpected) + "，实际 " + Long.toHexString(crcActual));
        }
        String hashId = new String(data, HEADER_LEN, hashLen, StandardCharsets.UTF_8);
        byte[] regs = new byte[m];
        System.arraycopy(data, HEADER_LEN + hashLen, regs, 0, m);
        int maxRank = 65 - p;
        for (int i = 0; i < m; i++) {
            int r = regs[i] & 0xff;
            if (r > maxRank) {
                throw new SketchFormatException(
                        "寄存器 #" + i + " 值越界: " + r + "（p=" + p + " 时最大 rank 为 " + maxRank + "）");
            }
        }
        return new HllSketch(new HllConfig(p, hashId), regs);
    }

    private static long crc(byte[] hashBytes, byte[] regs) {
        CRC32 crc = new CRC32();
        crc.update(hashBytes);
        crc.update(regs);
        return crc.getValue();
    }

    private static long crcRange(byte[] data, int off, int len) {
        CRC32 crc = new CRC32();
        crc.update(data, off, len);
        return crc.getValue();
    }
}
