package partjoin.test;

import partjoin.Json;
import partjoin.Row;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Multiset comparison of join outputs. Rows are canonicalized through their
 * JSON form (so numerically-equal wire representations coincide) and counted
 * by multiplicity - exactly what SQL join semantics require.
 */
final class Multiset {

    private Multiset() {
    }

    static Map<String, Long> of(List<Row> rows) {
        Map<String, Long> m = new HashMap<>();
        for (Row r : rows) m.merge(canonical(r), 1L, Long::sum);
        return m;
    }

    static void assertEqualMultisets(List<Row> actual, List<Row> expected, String context) {
        Map<String, Long> a = of(actual);
        Map<String, Long> e = of(expected);
        if (a.equals(e)) return;

        List<String> missing = new ArrayList<>();
        List<String> extra = new ArrayList<>();
        for (Map.Entry<String, Long> en : e.entrySet()) {
            long av = a.getOrDefault(en.getKey(), 0L);
            if (av < en.getValue()) {
                missing.add(en.getKey() + " x" + (en.getValue() - av));
            }
        }
        for (Map.Entry<String, Long> en : a.entrySet()) {
            long ev = e.getOrDefault(en.getKey(), 0L);
            if (ev < en.getValue()) {
                extra.add(en.getKey() + " x" + (en.getValue() - ev));
            }
        }
        String firstMissing = missing.stream().limit(5).toList().toString();
        String firstExtra = extra.stream().limit(5).toList().toString();
        throw new AssertionError(context + ": multisets differ (sizes actual="
                + actual.size() + " expected=" + expected.size()
                + ", distinct actual=" + a.size() + " expected=" + e.size() + ")"
                + "\n  missing (first 5): " + firstMissing
                + "\n  extra   (first 5): " + firstExtra);
    }

    private static String canonical(Row r) {
        return Json.write(r.rawList());
    }
}
