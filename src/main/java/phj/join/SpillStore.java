package phj.join;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.atomic.AtomicLong;

/**
 * 溢写磁盘管理器：临时目录、字节额度（{@code quotaBytes}，-1 表示不限）、
 * 当前/峰值占用统计。保留策略由调用方决定（keepSpillFiles）。
 */
public final class SpillStore implements AutoCloseable {

    private final Path dir;
    private final long quotaBytes; // -1 不限
    private final boolean keepFiles;
    private final AtomicLong liveBytes = new AtomicLong(0);
    private final AtomicLong peakBytes = new AtomicLong(0);
    private long fileSeq;
    private boolean dirCreated;
    private boolean closed;

    public SpillStore(Path baseDir, long quotaBytes, boolean keepFiles, String runTag) {
        this.quotaBytes = quotaBytes;
        this.keepFiles = keepFiles;
        this.dir = baseDir.resolve("phj-spill-" + runTag);
    }

    public Path dir() { return dir; }

    public long liveBytes() { return liveBytes.get(); }

    public long peakBytes() { return peakBytes.get(); }

    public long quotaBytes() { return quotaBytes; }

    public boolean isKeepFiles() { return keepFiles; }

    private void ensureDir() {
        if (!dirCreated) {
            try {
                Files.createDirectories(dir);
            } catch (IOException e) {
                throw new UncheckedIOException("无法创建溢写目录 " + dir, e);
            }
            dirCreated = true;
        }
    }

    void reserve(long n, String label) {
        long cur = liveBytes.addAndGet(n);
        peakBytes.accumulateAndGet(cur, Math::max);
        if (quotaBytes >= 0 && cur > quotaBytes) {
            // 回退本次预留，使错误对象里的 usedBytes 反映“未写入”状态
            liveBytes.addAndGet(-n);
            throw new DiskQuotaException(quotaBytes, Math.max(0, cur - n),
                    "磁盘溢写额度耗尽，无法写入分区 '" + label + "'（本次需要 " + n + " 字节）");
        }
    }

    void release(long n) {
        liveBytes.addAndGet(-n);
    }

    public SpillFile createFile(String label) {
        ensureDir();
        long id;
        synchronized (this) {
            id = fileSeq++;
        }
        String safe = label.replaceAll("[^A-Za-z0-9_.-]", "_");
        Path p = dir.resolve(String.format("%04d-%s.jsonl", id, safe));
        return new SpillFile(this, p, label);
    }

    /** 为左连接标记位图预留 n 字节，返回标记文件路径（由 MarkerFile 管理生命周期）。 */
    public Path reserveMarker(long n, String label) {
        ensureDir();
        reserve(n, label);
        long id;
        synchronized (this) {
            id = fileSeq++;
        }
        String safe = label.replaceAll("[^A-Za-z0-9_.-]", "_");
        return dir.resolve(String.format("%04d-%s.mark", id, safe));
    }

    @Override
    public void close() {
        if (closed) return;
        closed = true;
        if (!keepFiles && dirCreated) {
            try (var paths = Files.walk(dir)) {
                paths.sorted(java.util.Comparator.reverseOrder())
                        .forEach(p -> {
                            try { Files.deleteIfExists(p); }
                            catch (IOException ignored) { /* 尽力删除 */ }
                        });
            } catch (IOException e) {
                throw new UncheckedIOException("清理溢写目录失败 " + dir, e);
            }
        }
    }
}
