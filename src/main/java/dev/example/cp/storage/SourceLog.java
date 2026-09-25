package dev.example.cp.storage;

import dev.example.cp.core.Event;
import dev.example.cp.json.Json;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * 仅追加的本地输入日志（source/events.log，JSON Lines）。
 *
 * <p>它扮演“输入偏移（input offset）”语义中的分区日志：第 n 行偏移即 n。
 * 行采用先整行缓冲再一次性写入的方式追加；读到不完整的最后一行（崩溃残留）时跳过该行，
 * 恢复后新追加的事件从该行号开始，偏移语义仍然连续。
 */
public final class SourceLog {

    private final Path file;

    public SourceLog(Path dataDir) {
        this.file = dataDir.resolve("source").resolve("events.log");
    }

    public Path path() {
        return file;
    }

    /** 追加一个事件（JSONL），其偏移由当前行数决定。返回该事件的偏移。 */
    public synchronized long append(Event event) {
        truncateTornTail();
        long offset = count();
        Event stamped = new Event(offset, event.key(), event.value());
        DurableFiles.appendLine(file, Json.write(toJson(stamped)));
        return offset;
    }

    /**
     * 如果最后一行不是完整 JSON（崩溃残留的半截写），将其截断，保证后续追加从行边界开始。
     */
    private void truncateTornTail() {
        if (!Files.exists(file)) {
            return;
        }
        try {
            byte[] bytes = Files.readAllBytes(file);
            int lastNl = -1;
            for (int i = 0; i < bytes.length; i++) {
                if (bytes[i] == (byte) '\n') {
                    lastNl = i;
                }
            }
            String tail = new String(bytes, lastNl + 1, bytes.length - lastNl - 1, StandardCharsets.UTF_8).trim();
            if (tail.isEmpty()) {
                return; // 最后一个换行后没有内容，行边界干净
            }
            boolean valid;
            try {
                Json.parseObject(tail);
                valid = true;
            } catch (RuntimeException bad) {
                valid = false;
            }
            if (!valid) {
                try (var raf = new java.io.RandomAccessFile(file.toFile(), "rw");
                     var ch = raf.getChannel()) {
                    ch.truncate(lastNl + 1L);
                    ch.force(true);
                }
            }
        } catch (IOException e) {
            throw new StorageException("tail normalization failed: " + file, e);
        }
    }

    /** 批量追加，按顺序赋予连续偏移。 */
    public synchronized void appendAll(List<Event> events) {
        for (Event e : events) {
            append(e);
        }
    }

    /** 读取日志中的全部完整记录（偏移 = 数组下标）。 */
    public synchronized List<Event> readAll() {
        List<Event> events = new ArrayList<>();
        if (!Files.exists(file)) {
            return events;
        }
        List<String> lines;
        try {
            lines = Files.readAllLines(file, StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new StorageException("cannot read source log", e);
        }
        for (int i = 0; i < lines.size(); i++) {
            String line = lines.get(i).trim();
            if (line.isEmpty()) {
                continue;
            }
            try {
                events.add(Event.fromJson(Json.parseObject(line), i));
            } catch (RuntimeException ex) {
                // 崩溃可能留下不完整的最后一行：它只能是物理上的最后一行，跳过并停止。
                if (i == lines.size() - 1) {
                    break;
                }
                throw new StorageException("corrupt source log line " + i + ": " + ex.getMessage(), ex);
            }
        }
        return events;
    }

    /** 当前有效行数（= 下一条事件的偏移）。 */
    public synchronized long count() {
        return readAll().size();
    }

    static java.util.Map<String, Object> toJson(Event e) {
        java.util.LinkedHashMap<String, Object> m = new java.util.LinkedHashMap<>();
        m.put("offset", e.offset());
        m.put("key", e.key());
        m.put("value", e.value());
        return m;
    }
}
