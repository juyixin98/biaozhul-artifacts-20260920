package phj.join;

import phj.core.Row;

import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * 溢写分区文件：追加写 + 顺序读，按 UTF-8 字节计入磁盘额度。
 * 行缓冲落盘，文件删除后字节数回收到 SpillStore 的当前占用里。
 */
public final class SpillFile implements AutoCloseable {

    private final SpillStore store;
    private final Path path;
    private final String label;
    private final boolean kept;
    private BufferedWriter writer;
    private long bytes;
    private long rowsWritten;
    private boolean closedWriter;

    SpillFile(SpillStore store, Path path, String label) {
        this.store = store;
        this.path = path;
        this.label = label;
        this.kept = store.isKeepFiles();
        try {
            this.writer = Files.newBufferedWriter(path, StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new UncheckedIOException("无法创建溢写文件 " + path, e);
        }
    }

    public Path path() { return path; }

    public String label() { return label; }

    public long bytes() { return bytes; }

    public long rowCount() { return rowsWritten; }

    /** 追加一行；写入前做额度检查，超限抛 DiskQuotaException。 */
    public synchronized void appendRow(Row row) {
        String line = RowCodec.encode(row);
        byte[] raw = (line + "\n").getBytes(StandardCharsets.UTF_8);
        store.reserve(raw.length, label);
        try {
            writer.write(line);
            writer.newLine();
        } catch (IOException e) {
            throw new UncheckedIOException("写溢写文件失败 " + path, e);
        }
        bytes += raw.length;
        rowsWritten++;
    }

    /** 批量追加（保持与逐行相同的额度语义，但减少系统调用次数）。 */
    public void appendRows(List<Row> rows) {
        if (rows.isEmpty()) return;
        StringBuilder sb = new StringBuilder();
        long total = 0;
        for (Row r : rows) {
            String line = RowCodec.encode(r);
            sb.append(line).append('\n');
            total += utf8Length(line) + 1; // +1 换行符（ASCII）
        }
        store.reserve(total, label);
        try {
            writer.write(sb.toString());
        } catch (IOException e) {
            throw new UncheckedIOException("写溢写文件失败 " + path, e);
        }
        bytes += total;
        rowsWritten += rows.size();
    }

    private static long utf8Length(String s) {
        return s.getBytes(StandardCharsets.UTF_8).length;
    }

    public void flush() {
        try {
            writer.flush();
        } catch (IOException e) {
            throw new UncheckedIOException("flush 溢写文件失败 " + path, e);
        }
    }

    /** 关闭写句柄，之后才可读取。 */
    public void finishWriting() {
        if (!closedWriter) {
            try {
                writer.close();
            } catch (IOException e) {
                throw new UncheckedIOException("关闭溢写文件失败 " + path, e);
            }
            closedWriter = true;
        }
    }

    /** 顺序读全部行。 */
    public List<Row> readAll() {
        finishWriting();
        List<Row> out = new ArrayList<>();
        try (BufferedReader br = Files.newBufferedReader(path, StandardCharsets.UTF_8)) {
            String line;
            while ((line = br.readLine()) != null) {
                if (!line.isEmpty()) out.add(RowCodec.decode(line));
            }
        } catch (IOException e) {
            throw new UncheckedIOException("读溢写文件失败 " + path, e);
        }
        return out;
    }

    /** 流式读（大分区 / BNL 回退用），逐行回调。 */
    public void forEachRow(java.util.function.Consumer<Row> consumer) {
        finishWriting();
        try (BufferedReader br = Files.newBufferedReader(path, StandardCharsets.UTF_8)) {
            String line;
            while ((line = br.readLine()) != null) {
                if (!line.isEmpty()) consumer.accept(RowCodec.decode(line));
            }
        } catch (IOException e) {
            throw new UncheckedIOException("读溢写文件失败 " + path, e);
        }
    }

    public long lineCount() {
        final long[] n = {0};
        forEachRow(r -> n[0]++);
        return n[0];
    }

    /**
     * 真正的流式迭代器：内部持有 BufferedReader，逐行解码，
     * 供有界回退（BNL）逐块读取建表文件，不把整个文件载入内存。
     */
    public java.util.Iterator<Row> streamingIterator() {
        finishWriting();
        final BufferedReader br;
        try {
            br = Files.newBufferedReader(path, StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new UncheckedIOException("打开溢写文件失败 " + path, e);
        }
        return new java.util.Iterator<>() {
            private String nextLine = advance();

            private String advance() {
                try {
                    String line;
                    while ((line = br.readLine()) != null) {
                        if (!line.isEmpty()) return line;
                    }
                    br.close();
                    return null;
                } catch (IOException e) {
                    throw new UncheckedIOException("流式读溢写文件失败 " + path, e);
                }
            }

            @Override
            public boolean hasNext() { return nextLine != null; }

            @Override
            public Row next() {
                if (nextLine == null) throw new java.util.NoSuchElementException();
                Row r = RowCodec.decode(nextLine);
                nextLine = advance();
                return r;
            }
        };
    }

    /**
     * 逻辑删除：默认从磁盘删掉并归还额度；keepSpillFiles 时保留文件供检查 / 导出，
     * 但从 live 占用中移除（保留的文件不再参与后续额度计算，峰值仍已记录）。
     */
    public void delete() {
        finishWriting();
        if (kept) {
            store.release(bytes);
            return;
        }
        try {
            Files.deleteIfExists(path);
        } catch (IOException e) {
            throw new UncheckedIOException("删除溢写文件失败 " + path, e);
        }
        store.release(bytes);
        bytes = 0;
    }

    @Override
    public void close() {
        finishWriting();
    }
}
