package com.timeconv;

import com.timeconv.json.Json;
import com.timeconv.json.JsonNumber;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import static com.timeconv.TestRunner.assertEquals;
import static com.timeconv.TestRunner.test;

/**
 * Runs the fixed local test-data files in {@code data/cases/} against the service layer.
 * Each case is {"name", "endpoint", "request", "expect"} where expect is either
 * {"ok": true, "result": "...", "exact": ...} or {"ok": false, "code": "..."}.
 */
public final class CaseFileTest {

    public static void register(Path casesDir) {
        ConvertService service = new ConvertService();
        for (Path file : listCaseFiles(casesDir)) {
            List<?> cases = (List<?>) Json.parse(read(file));
            for (Object c : cases) {
                @SuppressWarnings("unchecked")
                Map<String, Object> kase = (Map<String, Object>) c;
                String name = file.getFileName() + ": " + kase.get("name");
                test(name, () -> runCase(service, kase));
            }
        }
    }

    @SuppressWarnings("unchecked")
    private static void runCase(ConvertService service, Map<String, Object> kase) {
        String endpoint = (String) kase.get("endpoint");
        Map<String, Object> request = (Map<String, Object>) kase.get("request");
        Map<String, Object> expect = (Map<String, Object>) kase.get("expect");

        Map<String, Object> actual;
        try {
            actual = switch (endpoint) {
                case "convert" -> service.convert(request);
                case "parse" -> service.parse(request);
                default -> throw new AssertionError("unknown endpoint: " + endpoint);
            };
        } catch (ConvertException e) {
            actual = ConvertService.error(e.code(), e.getMessage());
        }

        boolean expectOk = Boolean.TRUE.equals(expect.get("ok"));
        assertEquals(expectOk, actual.get("ok"));
        if (expectOk) {
            if (expect.containsKey("result")) {
                assertEquals(expect.get("result"), actual.get("result"));
            }
            if (expect.containsKey("exact")) {
                assertEquals(expect.get("exact"), actual.get("exact"));
            }
            if (expect.containsKey("unit")) {
                assertEquals(expect.get("unit"), actual.get("unit"));
            }
        } else {
            @SuppressWarnings("unchecked")
            Map<String, Object> err = (Map<String, Object>) actual.get("error");
            assertEquals(expect.get("code"), err.get("code"));
        }
    }

    private static List<Path> listCaseFiles(Path dir) {
        try (var stream = Files.list(dir)) {
            return stream.filter(p -> p.toString().endsWith(".json")).sorted().toList();
        } catch (IOException e) {
            throw new UncheckedIOException("cannot list case files in " + dir, e);
        }
    }

    private static String read(Path file) {
        try {
            return Files.readString(file);
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    private CaseFileTest() {
    }
}
