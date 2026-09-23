package cdcrebuild.engine;

import cdcrebuild.codec.Json;

import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.zip.CRC32;

/**
 * 单文件、只追加、每条记录带 CRC32 的事件日志（fsync 后才算写入成功）。
 *
 * 磁盘布局：
 *   文件头 : 8 字节魔数 "CDCWAL01"
 *   记录   : [int32 载荷长度][int32 CRC32][N 字节 UTF-8 JSON][int32 CRC32]
 *
 * 打开时从头扫描：遇到长度为 0 以外的截断 / CRC 不匹配，视为崩溃留下的半条记录，
 * 就地截断到最后一条完整记录边界（truncate），保证重放从干净状态开始。
 */
public final class Wal implements AutoCloseable {

    private static final byte[] MAGIC = "CDCWAL01".getBytes(StandardCharsets.US_ASCII);
    private static final int HEADER_LEN = 8;

    private final Path path;
    private final RandomAccessFile raf;
    private long length;

    private Wal(Path path, RandomAccessFile raf, long length) {
        this.path = path;
        this.raf = raf;
        this.length = length;
    }

    /** 打开已有日志或新建，修复半条尾部后返回；同时重放全部完整记录。 */
    public static Wal openAndRecover(Path path, List<Map<String, Object>> replaySink) throws IOException {
        Files.createDirectories(path.getParent() == null ? Path.of(".") : path.getParent());
        boolean fresh = !Files.exists(path) || Files.size(path) == 0;
        @SuppressWarnings("resource")
        RandomAccessFile raf = new RandomAccessFile(path.toFile(), "rw");
        long len;
        if (fresh) {
            raf.setLength(0);
            raf.seek(0);
            raf.write(MAGIC);
            raf.getFD().sync();
            len = HEADER_LEN;
        } else {
            len = raf.length();
            byte[] actualMagic = new byte[HEADER_LEN];
            raf.seek(0);
            int read = raf.read(actualMagic);
            if (read < HEADER_LEN || !java.util.Arrays.equals(actualMagic, MAGIC)) {
                throw new IOException("WAL 文件头损坏，拒绝打开: " + path);
            }
            len = scanAndRepair(raf, len, replaySink);
        }
        return new Wal(path, raf, len);
    }

    private static long scanAndRepair(RandomAccessFile raf, long fileLen,
                                      List<Map<String, Object>> sink) throws IOException {
        long pos = HEADER_LEN;
        List<Map<String, Object>> recovered = new ArrayList<>();
        while (pos < fileLen) {
            long frameStart = pos;
            if (fileLen - pos < 4) {
                return truncateTail(raf, frameStart, recovered, sink,
                        "截断的长度字段（仅 " + (fileLen - pos) + " 字节）");
            }
            raf.seek(pos);
            int payloadLen = raf.readInt();
            if (payloadLen <= 0 || payloadLen > 64 * 1024 * 1024) {
                return truncateTail(raf, frameStart, recovered, sink,
                        "非法载荷长度 " + payloadLen);
            }
            long frameTotal = 4L + 4L + payloadLen + 4L;
            if (fileLen - pos < frameTotal) {
                return truncateTail(raf, frameStart, recovered, sink,
                        "记录不完整（需要 " + frameTotal + " 字节，剩余 " + (fileLen - pos) + "）");
            }
            int expectedCrc = raf.readInt();
            byte[] payload = new byte[payloadLen];
            raf.readFully(payload);
            int trailingCrc = raf.readInt();
            long actualCrc = crc(payload);
            if (expectedCrc != (int) actualCrc || trailingCrc != (int) actualCrc) {
                return truncateTail(raf, frameStart, recovered, sink, "CRC32 校验失败");
            }
            String json = new String(payload, StandardCharsets.UTF_8);
            recovered.add(Json.parseObject(json));
            pos += frameTotal;
        }
        sink.addAll(recovered);
        return pos;
    }

    private static long truncateTail(RandomAccessFile raf, long goodEnd,
                                     List<Map<String, Object>> recovered,
                                     List<Map<String, Object>> sink, String reason) throws IOException {
        // 截断点之前的完整记录仍然有效，交给上层重放
        sink.addAll(recovered);
        raf.setLength(goodEnd);
        raf.getFD().sync();
        raf.seek(goodEnd);
        System.err.println("[WAL] 检测到损坏尾部并截断到字节 " + goodEnd + "（原因: " + reason + "）");
        return goodEnd;
    }

    /** 追加一条事件 JSON，强制落盘后返回。调用方保证 pos 单调连续。 */
    public synchronized void append(Map<String, Object> event) throws IOException {
        byte[] payload = Json.write(event).getBytes(StandardCharsets.UTF_8);
        int crc = (int) crc(payload);
        raf.seek(length);
        raf.writeInt(payload.length);
        raf.writeInt(crc);
        raf.write(payload);
        raf.writeInt(crc);
        raf.getFD().sync();
        length += 4L + 4L + payload.length + 4L;
    }

    public synchronized long lengthBytes() {
        return length;
    }

    private static long crc(byte[] data) {
        CRC32 c = new CRC32();
        c.update(data);
        return c.getValue();
    }

    @Override
    public synchronized void close() throws IOException {
        raf.close();
    }
}
