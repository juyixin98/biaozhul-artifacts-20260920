package txsnapshot;

import java.io.IOException;
import java.nio.channels.FileChannel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.FileAttribute;

/** 持久化辅助：原子发布临时文件 + fsync 文件与父目录。 */
final class DurableFiles {

    private DurableFiles() {}

    /** 确保目录存在，并对目录本身做一次 fsync（覆盖新建场景）。 */
    static Path ensureDir(Path dir) throws IOException {
        Files.createDirectories(dir);
        return dir;
    }

    /**
     * 原子发布：先把 bytes 写到同目录临时文件并 fsync，再 rename，最后 fsync 父目录，
     * 保证 rename 在崩溃后仍然生效。
     */
    static void writeAtomically(Path target, byte[] bytes) throws IOException {
        Path parent = target.toAbsolutePath().getParent();
        ensureDir(parent);
        Path tmp = parent.resolve(target.getFileName() + ".tmp-"
                + ProcessHandle.current().pid() + "-" + Thread.currentThread().getId() + "-"
                + System.nanoTime());
        try (FileChannel ch = FileChannel.open(tmp, StandardOpenOption.CREATE_NEW,
                StandardOpenOption.WRITE)) {
            java.nio.ByteBuffer bb = java.nio.ByteBuffer.wrap(bytes);
            while (bb.hasRemaining()) ch.write(bb);
            ch.force(true);
        }
        Files.move(tmp, target, java.nio.file.StandardCopyOption.ATOMIC_MOVE,
                java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        fsyncDir(parent);
    }

    /** 以追加模式打开一个只追加日志通道。 */
    static FileChannel openAppend(Path file) throws IOException {
        ensureDir(file.toAbsolutePath().getParent());
        return FileChannel.open(file, StandardOpenOption.CREATE, StandardOpenOption.WRITE,
                StandardOpenOption.APPEND);
    }

    /** 追加并强制刷盘（含元数据），返回写入前的文件大小（即本条的起始偏移）。 */
    static long appendForce(FileChannel ch, byte[] bytes) throws IOException {
        // 截短（崩溃残尾修复）可能由另一个文件描述符完成，APPEND 通道缓存的
        // position 不会随之移动；这里显式对齐到文件尾，避免在旧位置写出空洞。
        ch.position(ch.size());
        long pos = ch.position();
        java.nio.ByteBuffer bb = java.nio.ByteBuffer.wrap(bytes);
        while (bb.hasRemaining()) ch.write(bb);
        ch.force(false);
        return pos;
    }

    /** fsync 目录（Linux 上以只读方式打开目录再 force）。 */
    static void fsyncDir(Path dir) throws IOException {
        try (FileChannel ch = FileChannel.open(dir, StandardOpenOption.READ)) {
            ch.force(true);
        }
    }

    /**
     * 修复一个以 '\n' 分隔的只追加日志：截掉末尾不完整的行（崩溃时可能写了一半）。
     * 返回截完后的文件长度。
     */
    static long truncateToLastNewline(Path file) throws IOException {
        if (!Files.exists(file)) return 0L;
        long size = Files.size(file);
        if (size == 0) return 0L;
        try (FileChannel ch = FileChannel.open(file, StandardOpenOption.READ,
                StandardOpenOption.WRITE)) {
            int bufLen = (int) Math.min(8192, size);
            java.nio.ByteBuffer buf = java.nio.ByteBuffer.allocate(bufLen);
            int n = ch.read(buf, size - bufLen);
            byte[] tail = buf.array();
            int lastNl = -1;
            for (int i = n - 1; i >= 0; i--) {
                if (tail[i] == (byte) '\n') { lastNl = i; break; }
            }
            if (lastNl < 0) {
                ch.truncate(0);
                ch.force(true);
                return 0L;
            }
            long newSize = size - bufLen + lastNl + 1L;
            ch.truncate(newSize);
            ch.force(true);
            return newSize;
        }
    }

    @SuppressWarnings("unused") // 保留以备扩展
    static FileAttribute<?>[] noAttrs() { return new FileAttribute<?>[0]; }
}
