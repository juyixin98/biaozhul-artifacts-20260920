package com.example.txflow;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;

/**
 * 文件持久化原语：
 *
 *  - writeAtomically：先写同目录临时文件，fsync 后 rename 覆盖目标，
 *    再 fsync 父目录保证目录项落盘。崩溃后目标文件要么是旧内容、要么是新内容，
 *    绝不会出现半个文件。
 *  - fsyncParent：rename 后必须 fsync 父目录，否则崩溃可能丢失目录项
 *    （ext4 等日志文件系统默认只保证元数据日志，但我们显式做全）。
 */
public final class FileIO {

    private FileIO() {
    }

    public static void writeAtomically(Path target, byte[] content) throws IOException {
        Path dir = target.toAbsolutePath().getParent();
        Files.createDirectories(dir);
        Path tmp = Files.createTempFile(dir, target.getFileName() + ".", ".tmp");
        try {
            Files.write(tmp, content,
                    StandardOpenOption.WRITE,
                    StandardOpenOption.TRUNCATE_EXISTING,
                    StandardOpenOption.SYNC);
            tryFsync(tmp);
            Files.move(tmp, target, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            fsyncParent(target);
        } finally {
            // move 成功后 tmp 已不存在；失败时清理
            Files.deleteIfExists(tmp);
        }
    }

    public static void writeAtomically(Path target, String content) throws IOException {
        writeAtomically(target, content.getBytes(StandardCharsets.UTF_8));
    }

    /** 以 append + rws 语义追加（每次调用都 fsync，牺牲性能换取明确的持久化边界）。 */
    public static void appendDurably(Path target, byte[] content) throws IOException {
        Files.createDirectories(target.toAbsolutePath().getParent());
        try (java.nio.channels.FileChannel ch = java.nio.channels.FileChannel.open(target,
                StandardOpenOption.CREATE, StandardOpenOption.WRITE, StandardOpenOption.APPEND)) {
            ch.write(java.nio.ByteBuffer.wrap(content));
            ch.force(true);
        }
    }

    public static String readString(Path p) throws IOException {
        return new String(Files.readAllBytes(p), StandardCharsets.UTF_8);
    }

    public static byte[] readBytes(Path p) throws IOException {
        return Files.readAllBytes(p);
    }

    private static void tryFsync(Path p) {
        // Files.write 已带 SYNC；此处再对 channel 做一次 force 双保险。
        try (java.nio.channels.FileChannel ch = java.nio.channels.FileChannel.open(p,
                StandardOpenOption.WRITE, StandardOpenOption.READ)) {
            ch.force(true);
        } catch (IOException ignore) {
            // 某些文件系统不支持 fsync，忽略但不影响 rename 原子性
        }
    }

    public static void fsyncParent(Path p) {
        Path dir = p.toAbsolutePath().getParent();
        if (dir == null) {
            return;
        }
        try (java.nio.channels.FileChannel ch = java.nio.channels.FileChannel.open(dir,
                StandardOpenOption.READ)) {
            ch.force(true);
        } catch (IOException ignore) {
            // 部分文件系统/容器卷不支持目录 fsync
        }
    }
}
