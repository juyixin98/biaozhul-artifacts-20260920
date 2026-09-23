package com.example.txflow;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.List;

/**
 * 输入源：单机 append-only 记录日志（input.log）。
 *
 * 帧格式：[8 字节大端长度][UTF-8 JSON 载荷]，追加写入并 force 落盘。
 * offset 即记录序号（0,1,2,...），由追加顺序天然确定。
 *
 * 重放语义：offset 之后的所有记录可被确定性重读，这是
 * “状态 + 输入偏移 + 输出提交标记一致快照”能成立的前提。
 */
public class InputLog {

    private final Path file;

    public InputLog(Path dataDir) throws IOException {
        java.nio.file.Files.createDirectories(dataDir);
        this.file = dataDir.resolve("input.log");
        if (!java.nio.file.Files.exists(this.file)) {
            java.nio.file.Files.createFile(this.file);
        }
    }

    /** 追加一条输入记录，返回其 offset。 */
    public synchronized long append(String jsonRecord) throws IOException {
        byte[] payload = jsonRecord.getBytes(StandardCharsets.UTF_8);
        long offset = countRecords();
        ByteBuffer frame = ByteBuffer.allocate(8 + payload.length);
        frame.putLong(payload.length);
        frame.put(payload);
        frame.flip();
        try (FileChannel ch = openAppend()) {
            while (frame.hasRemaining()) {
                ch.write(frame);
            }
            ch.force(true);
        }
        return offset;
    }

    private FileChannel openAppend() throws IOException {
        return FileChannel.open(file,
                StandardOpenOption.CREATE, StandardOpenOption.WRITE, StandardOpenOption.APPEND);
    }

    /** 读取全部记录（按 offset 顺序）。 */
    public synchronized List<String> readAll() throws IOException {
        List<String> out = new ArrayList<>();
        try (FileChannel ch = FileChannel.open(file, StandardOpenOption.READ)) {
            ByteBuffer lenBuf = ByteBuffer.allocate(8);
            while (true) {
                lenBuf.clear();
                int n = readFully(ch, lenBuf);
                if (n == 0) {
                    break; // 干净的文件尾
                }
                if (n < 8) {
                    throw new IOException("input.log 损坏：尾部长度帧不完整（崩溃发生在追加途中），可读记录 "
                            + out.size() + " 条，残缺字节 " + n);
                }
                lenBuf.flip();
                int payloadLen = (int) lenBuf.getLong();
                if (payloadLen < 0 || payloadLen > 64 * 1024 * 1024) {
                    throw new IOException("input.log 损坏：非法帧长度 " + payloadLen);
                }
                ByteBuffer payload = ByteBuffer.allocate(payloadLen);
                int pn = readFully(ch, payload);
                if (pn < payloadLen) {
                    throw new IOException("input.log 损坏：尾部载荷不完整，已完整记录 " + out.size() + " 条");
                }
                payload.flip();
                out.add(new String(payload.array(), StandardCharsets.UTF_8));
            }
        }
        return out;
    }

    /** 统计已完整落盘的记录数（即下一条记录的 offset）。 */
    public synchronized long countRecords() throws IOException {
        return readAll().size();
    }

    private static int readFully(FileChannel ch, ByteBuffer buf) throws IOException {
        int total = 0;
        while (buf.hasRemaining()) {
            int n = ch.read(buf);
            if (n == -1) {
                break;
            }
            total += n;
        }
        return total;
    }
}
