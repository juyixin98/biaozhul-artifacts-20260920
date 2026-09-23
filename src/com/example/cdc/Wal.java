package com.example.cdc;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.List;

/**
 * 仅追加（append-only）的预写日志：每行一个 JSON 事件，UTF-8。
 *
 * <p>持久化是事务消费的事务边界前提：{@link #append(String)} 每次写完都 force(fsync)，
 * 进程在任意时刻崩溃，已返回成功的事件都不会丢。
 *
 * <p>本实现每次追加原子写入 {@code json + '\n'} 并 fsync，因此“完整记录”必然以换行结尾。
 * 打开时逐行校验：最后一行若没有换行结尾，或某行无法解析为 JSON 事件，
 * 一律视为崩溃时写了一半的残行（torn record），直接截断——绝不跳过继续。
 */
public final class Wal implements AutoCloseable {

    private final Path file;
    private final java.nio.channels.FileChannel ch;

    private Wal(Path file, java.nio.channels.FileChannel ch) {
        this.file = file;
        this.ch = ch;
    }

    /** 打开 WAL；不存在则创建，存在则扫描修复残行后定位到文件末尾。 */
    public static Wal open(Path path) {
        try {
            Files.createDirectories(path.toAbsolutePath().getParent());
            long validLength = 0;
            if (Files.exists(path)) {
                validLength = scan(path);
            }
            java.nio.channels.FileChannel ch = java.nio.channels.FileChannel.open(path,
                    StandardOpenOption.CREATE, StandardOpenOption.WRITE, StandardOpenOption.READ);
            ch.position(validLength);
            return new Wal(path, ch);
        } catch (IOException e) {
            throw new UncheckedIOException("打开 WAL 失败: " + path, e);
        }
    }

    /**
     * 逐行校验，返回“已确认完整”的字节长度（末尾残行被截断）。
     * 仅以 '\n' 结尾且能解析为事件的行才算完整记录。
     */
    private static long scan(Path path) throws IOException {
        byte[] bytes = Files.readAllBytes(path);
        long validLen = 0;
        int lineStart = 0;
        for (int i = 0; i < bytes.length; i++) {
            if (bytes[i] != '\n') {
                continue;
            }
            int end = i;
            if (end > lineStart && bytes[end - 1] == '\r') {
                end--;
            }
            if (end > lineStart) {
                String line = new String(bytes, lineStart, end - lineStart, StandardCharsets.UTF_8);
                try {
                    Event.fromRaw(line);
                } catch (RuntimeException bad) {
                    break; // 残行或坏行：其后内容一律不承认
                }
            }
            validLen = (long) i + 1;
            lineStart = i + 1;
        }
        if (validLen != bytes.length) {
            truncateTo(path, validLen);
        }
        return validLen;
    }

    private static void truncateTo(Path path, long length) throws IOException {
        try (java.nio.channels.FileChannel fc = java.nio.channels.FileChannel.open(path,
                StandardOpenOption.WRITE)) {
            fc.truncate(length);
        }
    }

    /** 读取 WAL 中全部事件（启动重放用）。 */
    public static List<Event> readAll(Path path) {
        List<Event> events = new ArrayList<>();
        if (!Files.exists(path)) {
            return events;
        }
        try {
            for (String line : Files.readAllLines(path, StandardCharsets.UTF_8)) {
                if (!line.isBlank()) {
                    events.add(Event.fromRaw(line));
                }
            }
        } catch (IOException e) {
            throw new UncheckedIOException("读取 WAL 失败: " + path, e);
        }
        return events;
    }

    /** 追加一行并 fsync，返回后保证已落盘。 */
    public synchronized void append(String rawJson) {
        try {
            byte[] data = (rawJson + "\n").getBytes(StandardCharsets.UTF_8);
            java.nio.ByteBuffer buf = java.nio.ByteBuffer.wrap(data);
            while (buf.hasRemaining()) {
                ch.write(buf);
            }
            ch.force(false);
        } catch (IOException e) {
            throw new UncheckedIOException("写 WAL 失败", e);
        }
    }

    /** 清空文件内容（/reset 使用）。 */
    public synchronized void reset() {
        try {
            ch.truncate(0);
            ch.position(0);
            ch.force(false);
        } catch (IOException e) {
            throw new UncheckedIOException("重置 WAL 失败", e);
        }
    }

    public Path path() {
        return file;
    }

    @Override
    public synchronized void close() {
        try {
            ch.force(false);
            ch.close();
        } catch (IOException e) {
            throw new UncheckedIOException("关闭 WAL 失败", e);
        }
    }
}
