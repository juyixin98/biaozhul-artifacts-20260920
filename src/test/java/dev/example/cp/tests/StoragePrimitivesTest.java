package dev.example.cp.tests;

import dev.example.cp.storage.DurableFiles;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

public class StoragePrimitivesTest extends TestCase {

    public StoragePrimitivesTest() {
        super("storage/atomic write never leaves a half file");
    }

    @Override
    protected void run() throws Exception {
        Path dir = newDataDir();
        Path target = dir.resolve("table.json");
        DurableFiles.writeAtomic(target, "v1");
        assertEquals("v1", Files.readString(target, StandardCharsets.UTF_8), "first write");
        DurableFiles.writeAtomic(target, "v2-longer-content");
        assertEquals("v2-longer-content", Files.readString(target, StandardCharsets.UTF_8), "second write");
        // 没有残留临时文件
        try (var list = Files.list(dir)) {
            assertTrue(list.noneMatch(p -> p.getFileName().toString().endsWith(".tmp")), "no tmp leftovers");
        }

        // appendLine 原子追加
        Path log = dir.resolve("events.log");
        DurableFiles.appendLine(log, "line1");
        DurableFiles.appendLine(log, "line2");
        var lines = Files.readAllLines(log, StandardCharsets.UTF_8);
        assertEquals(2, lines.size(), "appended lines");
        assertEquals("line2", lines.get(1), "second line content");
    }
}
