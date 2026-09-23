package phj.join;

import phj.core.Row;

import java.io.IOException;
import java.io.RandomAccessFile;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * 左连接在磁盘回退路径下的“已匹配”位图标记文件：
 * 第 i 个字节对应探测侧第 i 行，0 未匹配、1 已匹配。
 * 通过 RandomAccessFile 原地写字节，文件大小在首次写入前按额度预留。
 */
public final class MarkerFile implements AutoCloseable {

    private final SpillStore store;
    private final Path path;
    private final long size;
    private RandomAccessFile raf;
    private boolean reserved;
    private boolean closed;

    private MarkerFile(SpillStore store, Path path, long size) {
        this.store = store;
        this.path = path;
        this.size = size;
    }

    /** 创建一个可容纳 n 行标记的文件，按 n 字节计入磁盘额度。 */
    public static MarkerFile create(SpillStore store, String label, long n) {
        Path p = store.reserveMarker(n, label);
        MarkerFile mf = new MarkerFile(store, p, n);
        try {
            mf.raf = new RandomAccessFile(p.toFile(), "rw");
            mf.raf.setLength(n);
            mf.reserved = true;
        } catch (IOException e) {
            throw new UncheckedIOException("创建标记文件失败 " + p, e);
        }
        return mf;
    }

    public void mark(long index) {
        try {
            raf.seek(index);
            raf.write(1);
        } catch (IOException e) {
            throw new UncheckedIOException("写标记文件失败 " + path, e);
        }
    }

    public boolean isMarked(long index) {
        try {
            raf.seek(index);
            return raf.read() == 1;
        } catch (IOException e) {
            throw new UncheckedIOException("读标记文件失败 " + path, e);
        }
    }

    public long size() { return size; }

    @Override
    public void close() {
        if (closed) return;
        closed = true;
        try {
            raf.close();
        } catch (IOException e) {
            throw new UncheckedIOException("关闭标记文件失败 " + path, e);
        }
        // 标记位图是内部工作文件，无论 keepSpillFiles 与否都删除
        try {
            Files.deleteIfExists(path);
        } catch (IOException e) {
            throw new UncheckedIOException("删除标记文件失败 " + path, e);
        }
        if (reserved) store.release(size);
    }
}
