package com.example.cptx.core;

import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 持久输入日志（JSON Lines）：每个事件先 fsync 追加到 input.log 才被视为已接收。
 * 行号即偏移语义——第 N 行的 offset = N（0 基），与文件中已有条数一致。
 * 这是“输入偏移”检查点协议的事实来源。
 */
public final class InputLog {

    private final Path file;

    public InputLog(Path baseDir) throws IOException {
        Files.createDirectories(baseDir);
        this.file = baseDir.resolve("input.log");
        if (!Files.exists(file)) {
            Files.createFile(file);
        }
    }

    /** 追加一批事件，offset 由日志当前长度连续分配；整批 fsync 后返回带 offset 的事件。 */
    public synchronized List<Event> appendAll(List<Map<String, Object>> payloads) throws IOException {
        long base = count();
        StringBuilder sb = new StringBuilder();
        List<Event> events = new ArrayList<>(payloads.size());
        for (int i = 0; i < payloads.size(); i++) {
            Event e = Event.fromInput(payloads.get(i), base + i);
            events.add(e);
            sb.append(Json.write(e.toJson())).append('\n');
        }
        byte[] bytes = sb.toString().getBytes(StandardCharsets.UTF_8);
        try (FileOutputStream out = new FileOutputStream(file.toFile(), true)) {
            out.write(bytes);
            out.flush();
            out.getChannel().force(true);
        }
        return events;
    }

    /** 读取全部事件。小数据参考实现，一次性载入内存。 */
    public synchronized List<Event> readAll() throws IOException {
        List<Event> events = new ArrayList<>();
        long offset = 0;
        for (String line : Files.readAllLines(file, StandardCharsets.UTF_8)) {
            if (line.isBlank()) continue;
            Event e = Event.fromJson(Json.obj(Json.parse(line)));
            if (e.offset != offset) {
                throw new IOException("输入日志偏移不连续：期望 " + offset + "，实际 " + e.offset);
            }
            events.add(e);
            offset++;
        }
        return events;
    }

    public synchronized long count() throws IOException {
        try (var lines = Files.lines(file, StandardCharsets.UTF_8)) {
            return lines.filter(l -> !l.isBlank()).count();
        }
    }
}
