package join;

import java.io.BufferedReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** Shared brute-force reference implementation used by tests. */
public final class EngineTestsSupport {

    private EngineTestsSupport() {
    }

    /** Reference join multiset, key column "k", null/missing keys excluded. */
    public static Map<String, Long> referenceMultiset(List<String> left, List<String> right) {
        Map<Long, List<String>> lIndex = new HashMap<>();
        for (String line : left) {
            Object k = Json.parseObject(line).get("k");
            if (k instanceof Long) {
                lIndex.computeIfAbsent((Long) k, x -> new ArrayList<>()).add(line);
            }
        }
        Map<String, Long> expected = new HashMap<>();
        for (String r : right) {
            Object k = Json.parseObject(r).get("k");
            if (k instanceof Long) {
                List<String> ls = lIndex.get(k);
                if (ls != null) {
                    for (String l : ls) {
                        expected.merge("{\"left\":" + l + ",\"right\":" + r + "}", 1L, Long::sum);
                    }
                }
            }
        }
        return expected;
    }

    public static Map<String, Long> multisetFromFile(Path result) throws Exception {
        Map<String, Long> actual = new HashMap<>();
        try (BufferedReader br = Files.newBufferedReader(result, StandardCharsets.UTF_8)) {
            String line;
            while ((line = br.readLine()) != null) {
                if (!line.isEmpty()) {
                    actual.merge(line, 1L, Long::sum);
                }
            }
        }
        return actual;
    }
}
