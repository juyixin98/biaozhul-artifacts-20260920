package com.example.cptx.tests;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Comparator;

/** 测试临时目录：build/test-tmp/<name>-<nano>，每次全新。 */
public final class TestDirs {

    private static final Path ROOT = Path.of("build", "test-tmp");

    private TestDirs() {}

    public static Path create(String name) throws IOException {
        Files.createDirectories(ROOT);
        Path dir = ROOT.resolve(name + "-" + System.nanoTime());
        Files.createDirectories(dir);
        return dir;
    }

    public static void deleteAll() throws IOException {
        if (!Files.exists(ROOT)) return;
        try (var s = Files.walk(ROOT)) {
            s.sorted(Comparator.reverseOrder()).forEach(p -> {
                try { Files.deleteIfExists(p); } catch (IOException ignore) { }
            });
        }
    }
}
