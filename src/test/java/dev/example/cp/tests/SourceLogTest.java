package dev.example.cp.tests;

import dev.example.cp.core.Event;
import dev.example.cp.storage.SourceLog;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

public class SourceLogTest extends TestCase {

    public SourceLogTest() {
        super("source/offsets are line numbers and torn tails are trimmed");
    }

    @Override
    protected void run() throws Exception {
        Path dir = newDataDir();
        SourceLog log = new SourceLog(dir);
        log.appendAll(List.of(Event.of(-1, "a", 1), Event.of(-1, "b", 2), Event.of(-1, "a", 10)));
        List<Event> read = log.readAll();
        assertEquals(3, read.size(), "three events");
        assertEquals(2L, read.get(2).offset(), "offset is line index");
        assertEquals(10L, read.get(2).value(), "value round trip");

        // 模拟崩溃：手工写入一个不完整的半截最后一行
        Path file = log.path();
        Files.writeString(file, "{\"offset\":3,\"key\":\"a\",\"valu", StandardCharsets.UTF_8,
                java.nio.file.StandardOpenOption.APPEND);
        assertEquals(3, log.readAll().size(), "torn last line is ignored");

        // 恢复后追加：半截残尾被截断，新事件从偏移 3 开始
        long off = log.append(Event.of(-1, "c", 5));
        assertEquals(3L, off, "new event resumes at offset 3");
        List<Event> after = log.readAll();
        assertEquals(4, after.size(), "four good events after recovery");
        assertEquals("c", after.get(3).key(), "last event key");
    }
}
