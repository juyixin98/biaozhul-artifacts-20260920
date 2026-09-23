package bitmapindex;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

/**
 * Offline demo: generate a synthetic dataset, build the index, print space
 * statistics and run representative queries (including NOT after deletions).
 * Does not need the HTTP server.
 *
 *   java -cp build/classes bitmapindex.Demo [rows]
 */
public final class Demo {

    static final String[] CITIES = {"Beijing", "Shanghai", "Shenzhen", "Hangzhou", "Chengdu", "Xi'an"};
    static final String[] COLORS = {"red", "green", "blue", "amber", "violet"};

    public static void main(String[] args) {
        int n = args.length > 0 ? Integer.parseInt(args[0]) : 100_000;
        Random rnd = new Random(20260923);

        List<String> columns = List.of("city", "color", "active", "userId");
        List<Map<String, Object>> rows = new ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("city", CITIES[rnd.nextInt(CITIES.length)]);
            row.put("color", COLORS[rnd.nextInt(COLORS.length)]);
            row.put("active", rnd.nextInt(100) < 80 ? "yes" : "no");
            row.put("userId", "user-" + rnd.nextInt(n)); // high cardinality
            rows.add(row);
        }

        BitMapIndex index = new BitMapIndex();
        long t0 = System.nanoTime();
        index.load(rows, columns);
        long buildMs = (System.nanoTime() - t0) / 1_000_000;

        System.out.println("== Built index for " + n + " rows in " + buildMs + " ms ==");
        System.out.println(Json.write(index.stats()));

        Object red = Map.of("op", "eq", "column", "color", "value", "red");
        Object bj = Map.of("op", "eq", "column", "city", "value", "Beijing");
        Object active = Map.of("op", "eq", "column", "active", "value", "yes");
        Object expr = Map.of("op", "and", "args", List.of(red,
                Map.of("op", "or", "args", List.of(bj, active)),
                Map.of("op", "not", "arg", Map.of("op", "eq", "column", "color", "value", "violet"))));

        t0 = System.nanoTime();
        int hits = index.query(expr).cardinality();
        long queryUs = (System.nanoTime() - t0) / 1000;
        List<Integer> scan = index.scan(expr);
        System.out.println("nested AND/OR/NOT query: index=" + hits + " rows, scan=" + scan.size()
                + " rows, index query " + queryUs + " us, match=" + (hits == scan.size()));
        System.out.println("first 10 hit ids: " + preview(index.query(expr).toArray(), 10));

        // delete the first 10,000 rows and 5,000 random rows
        for (int i = 0; i < 10_000 && i < n; i++) {
            index.delete(i);
        }
        for (int k = 0; k < 5_000; k++) {
            index.delete(rnd.nextInt(n));
        }
        System.out.println("\n== After deletions: live=" + index.liveCount()
                + " / total=" + index.totalRows() + " ==");

        int afterHits = index.query(expr).cardinality();
        List<Integer> afterScan = index.scan(expr);
        System.out.println("same query after deletion: index=" + afterHits
                + " scan=" + afterScan.size() + " match=" + (afterHits == afterScan.size()));

        Object notRed = Map.of("op", "not", "arg", red);
        int notHits = index.query(notRed).cardinality();
        long redLive = index.query(red).cardinality();
        System.out.println("NOT(red)=" + notHits + " + red(live)=" + redLive
                + " = " + (notHits + redLive) + " == liveRows " + index.liveCount()
                + " -> " + (notHits + redLive == index.liveCount()));
        System.out.println("NOT(red) contains no deleted id: "
                + verifyNoDeleted(index, notRed, 10_000));
    }

    private static String preview(int[] ids, int limit) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < Math.min(limit, ids.length); i++) {
            if (i > 0) {
                sb.append(", ");
            }
            sb.append(ids[i]);
        }
        if (ids.length > limit) {
            sb.append(", ...");
        }
        return sb.append(']').toString();
    }

    private static boolean verifyNoDeleted(BitMapIndex index, Object expr, int firstDeleted) {
        for (int id : index.query(expr).toArray()) {
            if (id < firstDeleted || !index.isLive(id)) {
                return false;
            }
        }
        return true;
    }
}
