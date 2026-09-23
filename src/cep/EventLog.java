package cep;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.channels.FileChannel;
import java.nio.ByteBuffer;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 预写日志（WAL）。
 *
 * 存储格式：文件 {@code event.log}，每行一个 JSON 对象（NDJSON），
 * 按批次追加，一行即一个批次的全部事件。每次追加后 fsync（force(true)），
 * 保证进程被 kill -9 / 断电后，已确认（HTTP 200）的批次不会丢失。
 *
 * 崩溃恢复时从头顺序重放。崩溃只可能发生在“某一行写到一半”的状态：
 *   - 最后一行若不是完整合法的 NDJSON 记录，视为未完成写入，截断该行后重放
 *     其余全部完整记录（这是 WAL 的标准语义：未完成的批次从未向调用方返回过
 *     成功，截断它不是静默丢数据）。
 *   - 除“末尾不完整行”之外的任何损坏（中间行 JSON 非法、字段缺失等）
 *     都直接抛异常拒绝启动，绝不悄悄跳过。
 *
 * 本类只负责日志文件；事件到 JSON 的映射字段与 HTTP 接口无关，是内部格式。
 */
public final class EventLog implements AutoCloseable {

    private final Path logFile;
    private FileChannel channel;

    public EventLog(Path dataDir) {
        try {
            Files.createDirectories(dataDir);
            this.logFile = dataDir.resolve("event.log");
            this.channel = openAppendChannel();
        } catch (IOException e) {
            throw new UncheckedIOException("无法打开预写日志: " + dataDir, e);
        }
    }

    private FileChannel openAppendChannel() throws IOException {
        return FileChannel.open(logFile,
                StandardOpenOption.CREATE,
                StandardOpenOption.WRITE,
                StandardOpenOption.APPEND);
    }

    /**
     * 追加一个已分配好输入序号的批次并 fsync。
     * 记录格式：{"events":[{"type":..,"entityId":..,"timestamp":..,"seq":..}, ...]}
     * 此方法返回后，调用方才可向客户端确认成功。
     */
    public synchronized void appendBatch(List<Event> assigned) {
        List<Object> raw = new ArrayList<>(assigned.size());
        for (Event e : assigned) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("type", e.type);
            m.put("entityId", e.entityId);
            m.put("timestamp", e.timestamp);
            m.put("seq", e.seq);
            raw.add(m);
        }
        Map<String, Object> record = new LinkedHashMap<>();
        record.put("events", raw);
        byte[] payload = (Json.write(record) + "\n").getBytes(StandardCharsets.UTF_8);
        try {
            ByteBuffer wrap = ByteBuffer.wrap(payload);
            while (wrap.hasRemaining()) {
                channel.write(wrap);
            }
            channel.force(true);
        } catch (IOException e) {
            throw new UncheckedIOException("写入预写日志失败", e);
        }
    }

    /**
     * 重放全部完整批次，并在发现“末尾不完整行”时将其截断。
     *
     * 判定依据是换行边界而不是“能否解析”：每条完整记录都以 '\n' 结尾
     * （appendBatch 保证），而单条 JSON 记录内部不含裸换行。因此：
     *   - 最后一个 '\n' 之前的每一行都必须是完整合法记录，任一解析失败即
     *     “中间损坏”，抛异常拒绝启动；
     *   - 最后一个 '\n' 之后若还有字节，只可能是崩溃时写了一半的记录，
     *     物理截断后重放前面的完整批次。
     *
     * @return 按文件顺序排列的批次，每批事件已含崩溃前分配的序号
     */
    public synchronized List<List<Event>> replay() {
        byte[] bytes;
        try {
            bytes = Files.exists(logFile) ? Files.readAllBytes(logFile) : new byte[0];
        } catch (IOException e) {
            throw new UncheckedIOException("读取预写日志失败", e);
        }

        int lastNewline = -1;
        for (int i = 0; i < bytes.length; i++) {
            if (bytes[i] == '\n') {
                lastNewline = i;
            }
        }
        int completeLen = lastNewline + 1; // 最后一个换行符（含）之前的字节数

        List<List<Event>> batches = new ArrayList<>();
        if (completeLen > 0) {
            String completeText = new String(bytes, 0, completeLen, StandardCharsets.UTF_8);
            int lineNo = 0;
            int from = 0;
            while (from < completeText.length()) {
                int nl = completeText.indexOf('\n', from);
                if (nl < 0) {
                    break;
                }
                lineNo++;
                batches.add(parseRecordLine(completeText.substring(from, nl), lineNo));
                from = nl + 1;
            }
        }

        if (bytes.length > completeLen) {
            String tail = new String(bytes, completeLen, bytes.length - completeLen,
                    StandardCharsets.UTF_8);
            // 末尾不完整记录：它从未向调用方确认过，截断后继续。
            truncateTail(completeLen);
            if (tail.trim().isEmpty()) {
                throw new IllegalStateException(
                        "预写日志末尾存在无换行的空白片段，日志状态异常");
            }
        }
        return batches;
    }

    private static List<Event> parseRecordLine(String line, int lineNo) {
        if (line.isEmpty()) {
            throw new IllegalStateException(
                    "预写日志第 " + lineNo + " 行为空行，日志已损坏");
        }
        Object parsed;
        try {
            parsed = Json.parse(line);
        } catch (Json.JsonException ex) {
            throw new IllegalStateException(
                    "预写日志第 " + lineNo + " 行 JSON 非法，日志已损坏: "
                            + ex.getMessage(), ex);
        }
        if (!(parsed instanceof Map)) {
            throw new IllegalStateException(
                    "预写日志第 " + lineNo + " 行不是 JSON 对象，日志已损坏");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> rec = (Map<String, Object>) parsed;
        Object evObj = rec.get("events");
        if (!(evObj instanceof List)) {
            throw new IllegalStateException(
                    "预写日志第 " + lineNo + " 行缺少 events 数组，日志已损坏");
        }
        List<Event> batch = new ArrayList<>();
        for (Object item : (List<?>) evObj) {
            if (!(item instanceof Map)) {
                throw new IllegalStateException(
                        "预写日志第 " + lineNo + " 行事件格式非法，日志已损坏");
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> em = (Map<String, Object>) item;
            try {
                String type = (String) em.get("type");
                String entityId = (String) em.get("entityId");
                long ts = ((Number) em.get("timestamp")).longValue();
                long seq = ((Number) em.get("seq")).longValue();
                batch.add(new Event(type, entityId, ts, seq));
            } catch (RuntimeException ex) {
                throw new IllegalStateException(
                        "预写日志第 " + lineNo + " 行事件字段缺失或类型错误，日志已损坏", ex);
            }
        }
        return batch;
    }

    /** 把日志截断为前 {@code keepBytes} 个字节（保留完整记录，丢弃末尾半行）。 */
    private void truncateTail(long keepBytes) {
        try {
            channel.close();
            try (FileChannel ch = FileChannel.open(logFile,
                    StandardOpenOption.READ, StandardOpenOption.WRITE)) {
                ch.truncate(keepBytes);
                ch.position(keepBytes);
                ch.force(true);
            }
            // 不用 APPEND 重开：截断后 APPEND 在部分平台仍按旧 EOF 定位，
            // 会在中间留出空洞。改用可定位通道，写入位置显式置为新末尾。
            this.channel = FileChannel.open(logFile,
                    StandardOpenOption.CREATE,
                    StandardOpenOption.WRITE);
            this.channel.position(keepBytes);
        } catch (IOException e) {
            throw new UncheckedIOException("截断预写日志末尾不完整记录失败", e);
        }
    }

    /** 删除全部日志内容（对应 POST /reset）；调用方需先清空引擎内存状态。 */
    public synchronized void reset() {
        try {
            channel.close();
            Files.deleteIfExists(logFile);
            this.channel = openAppendChannel();
        } catch (IOException e) {
            throw new UncheckedIOException("重置预写日志失败", e);
        }
    }

    public Path path() {
        return logFile;
    }

    @Override
    public synchronized void close() {
        try {
            channel.force(true);
            channel.close();
        } catch (IOException e) {
            throw new UncheckedIOException("关闭预写日志失败", e);
        }
    }
}
