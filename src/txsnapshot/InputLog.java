package txsnapshot;

import java.io.IOException;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 输入日志（source-of-truth WAL）：每行一个 JSON：{"offset":N,"value":x}。
 * offset 从 0 开始连续递增。每次 append 后 fsync，保证已返回的输入在崩溃后仍可重放。
 * 打开时截掉末尾不完整行。
 */
public final class InputLog implements AutoCloseable {

    private final Path file;
    private FileChannel channel;
    private long nextOffset;

    private InputLog(Path file, long nextOffset, FileChannel channel) {
        this.file = file;
        this.nextOffset = nextOffset;
        this.channel = channel;
    }

    public static InputLog open(Path dir) throws IOException {
        DurableFiles.ensureDir(dir);
        Path file = dir.resolve("input.log");
        DurableFiles.truncateToLastNewline(file);
        List<Map<String, Object>> recs = readAll(file);
        long next = recs.isEmpty() ? 0L : ((Number) recs.get(recs.size() - 1).get("offset")).longValue() + 1;
        // 校验偏移连续，否则属于损坏
        for (int i = 0; i < recs.size(); i++) {
            long o = ((Number) recs.get(i).get("offset")).longValue();
            if (o != i) {
                throw new IOException("input.log gap/disorder at line " + i + " offset " + o);
            }
        }
        FileChannel ch = DurableFiles.openAppend(file);
        return new InputLog(file, next, ch);
    }

    /** 追加一条输入，返回其偏移。同步 fsync。 */
    public synchronized long append(long value) throws IOException {
        long offset = nextOffset;
        String line = Json.dump(Map.of("offset", offset, "value", value)) + "\n";
        DurableFiles.appendForce(channel, line.getBytes(StandardCharsets.UTF_8));
        nextOffset++;
        return offset;
    }

    public synchronized long nextOffset() {
        return nextOffset;
    }

    /** 读取偏移 >= fromOffset 的全部记录（用于重放）。 */
    public static List<long[]> readFrom(Path dir, long fromOffset) throws IOException {
        List<long[]> out = new ArrayList<>();
        for (Map<String, Object> rec : readAll(dir.resolve("input.log"))) {
            long off = ((Number) rec.get("offset")).longValue();
            long val = ((Number) rec.get("value")).longValue();
            if (off >= fromOffset) out.add(new long[]{off, val});
        }
        return out;
    }

    private static List<Map<String, Object>> readAll(Path file) throws IOException {
        List<Map<String, Object>> out = new ArrayList<>();
        if (!Files.exists(file)) return out;
        for (String line : Files.readString(file, StandardCharsets.UTF_8).split("\n", -1)) {
            if (line.isEmpty()) continue;
            out.add(Json.object(line));
        }
        return out;
    }

    @Override
    public synchronized void close() throws IOException {
        if (channel != null) {
            channel.close();
            channel = null;
        }
    }
}
