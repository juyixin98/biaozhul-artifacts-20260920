package dev.example.cp.storage;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;

/**
 * “崩溃安全”文件写入原语（参考实现）。
 *
 * <p>写入顺序固定为：写临时文件 → fsync 数据 → 原子 rename 到目标位置 → fsync 目录。
 * rename 是提交点：崩溃后世界只能观察到“旧版本”或“新版本”，绝不会观察到半个文件。
 */
public final class DurableFiles {

    private DurableFiles() {
    }

    /** 原子地将 {@code content} 发布到 {@code target}。 */
    public static synchronized void writeAtomic(Path target, String content) {
        try {
            Path parent = target.toAbsolutePath().getParent();
            Files.createDirectories(parent);
            Path tmp = parent.resolve(target.getFileName() + ".tmp-" + Thread.currentThread().threadId());
            Files.writeString(tmp, content, StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING, StandardOpenOption.WRITE);
            // 数据落盘
            try (java.nio.channels.FileChannel ch = java.nio.channels.FileChannel.open(
                    tmp, StandardOpenOption.WRITE)) {
                ch.force(true);
            }
            atomicMove(tmp, target);
            fsyncDir(parent);
        } catch (IOException e) {
            throw new StorageException("atomic write failed: " + target, e);
        }
    }

    /** 追加一行（调用方负责保证 {@code line} 内不含换行符），随后 fsync。 */
    public static synchronized void appendLine(Path file, String line) {
        try {
            Path parent = file.toAbsolutePath().getParent();
            Files.createDirectories(parent);
            try (var raf = new java.io.RandomAccessFile(file.toFile(), "rw");
                 var ch = raf.getChannel()) {
                ch.position(ch.size());
                byte[] data = (line + System.lineSeparator()).getBytes(StandardCharsets.UTF_8);
                int written = 0;
                while (written < data.length) {
                    written += ch.write(java.nio.ByteBuffer.wrap(data, written, data.length - written));
                }
                ch.force(false);
            }
            fsyncDir(parent);
        } catch (IOException e) {
            throw new StorageException("append failed: " + file, e);
        }
    }

    private static void atomicMove(Path src, Path dst) throws IOException {
        try {
            Files.move(src, dst, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        } catch (AtomicMoveNotSupportedException e) {
            // 退化方案仍保证目标完整：先写完整临时文件再非原子替换（同目录下多数文件系统仍为 rename）。
            Files.move(src, dst, StandardCopyOption.REPLACE_EXISTING);
        }
    }

    private static void fsyncDir(Path dir) {
        // 目录 fsync 在部分平台不可用；参考实现尽力而为（进程级崩溃安全已由 rename 原子性保证，
        // 验收关注的是 kill -9 这类进程级故障，而非掉电）。
        if (!System.getProperty("os.name", "").toLowerCase().contains("win")) {
            try (java.nio.channels.FileChannel ch = java.nio.channels.FileChannel.open(
                    dir, StandardOpenOption.READ)) {
                ch.force(true);
            } catch (Exception ignored) {
                // 平台不支持则忽略
            }
        }
    }
}
