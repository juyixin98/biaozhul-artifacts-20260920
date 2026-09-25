package com.example.cptx.core;

import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.channels.FileChannel;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.util.Comparator;
import java.util.List;
import java.util.stream.Stream;

/** 本地文件系统工具：原子写（tmp + rename）、目录 fsync、前缀列举。 */
final class FileIO {

    private FileIO() {}

    static void writeAtomic(Path target, byte[] data) throws IOException {
        Path tmp = target.resolveSibling(target.getFileName() + ".tmp." + ProcessHandle.current().pid());
        try (FileOutputStream out = new FileOutputStream(tmp.toFile())) {
            out.write(data);
            out.flush();
            out.getChannel().force(true);
        }
        try {
            Files.move(tmp, target, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        } catch (AtomicMoveNotSupportedException e) {
            // 退化到非原子 move（同目录下 rename 失败的罕见平台）
            Files.move(tmp, target, StandardCopyOption.REPLACE_EXISTING);
        }
        fsyncParent(target);
    }

    /** 列出目录下匹配前缀的文件（按文件名排序），目录不存在时返回空表。 */
    static List<Path> list(Path dir, String prefix) throws IOException {
        if (!Files.isDirectory(dir)) return List.of();
        try (Stream<Path> s = Files.list(dir)) {
            return s.filter(p -> p.getFileName().toString().startsWith(prefix))
                    .sorted(Comparator.comparing(p -> p.getFileName().toString()))
                    .toList();
        }
    }

    /** 删除目录下匹配前缀的文件（用于启动时清理残留的临时/暂存文件）。 */
    static void deleteWithPrefix(Path dir, String... prefixes) throws IOException {
        if (!Files.isDirectory(dir)) return;
        try (Stream<Path> s = Files.list(dir)) {
            List<Path> targets = s.filter(p -> {
                String n = p.getFileName().toString();
                for (String prefix : prefixes) {
                    if (n.startsWith(prefix)) return true;
                }
                return false;
            }).toList();
            for (Path t : targets) {
                Files.deleteIfExists(t);
            }
        }
    }

    static void fsyncDirectory(Path p) {
        Path dir = Files.isDirectory(p) ? p : p.toAbsolutePath().getParent();
        if (dir == null || !Files.isDirectory(dir)) return;
        try (FileChannel ch = FileChannel.open(dir, StandardOpenOption.READ)) {
            ch.force(true);
        } catch (IOException ignore) {
            // 部分平台不允许 fsync 目录（如 Windows），尽力而为。
        }
    }

    private static void fsyncParent(Path p) throws IOException {
        fsyncDirectory(p);
    }
}
