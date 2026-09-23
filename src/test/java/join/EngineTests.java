package join;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

/**
 * Correctness / spill / cancellation tests for the join engine itself
 * (no HTTP). Run via {@code test.sh}; exits non-zero on any failure.
 */
public class EngineTests {

    private int passed;
    private int failed;
    private final List<String> failures = new ArrayList<>();

    public static void main(String[] args) throws Exception {
        EngineTests t = new EngineTests();
        t.runAll();
    }

    private void runAll() throws Exception {
        testBasicSemantics();
        testRandomAgainstReference(1234, 2000, 1500, 1L << 30, false, "in-memory");
        testRandomAgainstReference(77, 3000, 2600, 512, true, "spill-tiny-budget");
        testRandomAgainstReference(99, 800, 700, 128, true, "many-runs-multipass");
        testHotKeyGroupLargerThanBudget();
        testTempCleanupOnSuccess();
        testInvalidJsonReported();
        testCancellationCleansTempFiles();
        testMissingKeyIsNull();

        System.out.println();
        System.out.println("Engine tests: " + passed + " passed, " + failed + " failed");
        if (failed > 0) {
            for (String f : failures) {
                System.out.println("FAIL: " + f);
            }
            System.exit(1);
        }
    }

    private void check(boolean cond, String name) {
        if (cond) {
            passed++;
            System.out.println("  ok   " + name);
        } else {
            failed++;
            failures.add(name);
            System.out.println("  FAIL " + name);
        }
    }

    // ------------------------------------------------------------- fixtures

    /** A generated JSONL table plus expected reference join multiset. */
    static final class TableFile {
        Path file;
        List<String> rows;
    }

    private TableFile writeTable(Path dir, String name, List<String> rows) throws IOException {
        Path f = dir.resolve(name);
        Files.write(f, rows, StandardCharsets.UTF_8);
        TableFile t = new TableFile();
        t.file = f;
        t.rows = rows;
        return t;
    }

    private String row(long key, int payloadSize, Random rnd) {
        StringBuilder sb = new StringBuilder("{\"id\":").append(rnd.nextLong() & Long.MAX_VALUE);
        sb.append(",\"k\":").append(key).append(",\"p\":\"");
        char[] c = new char[payloadSize];
        String alpha = "abcdefghijklmnopqrstuvwxyz";
        for (int i = 0; i < payloadSize; i++) {
            c[i] = alpha.charAt(rnd.nextInt(alpha.length()));
        }
        return sb.append(c).append("\"}").toString();
    }

    private String nullRow(Random rnd) {
        return "{\"id\":" + (rnd.nextLong() & Long.MAX_VALUE) + ",\"k\":null,\"p\":\"null-key\"}";
    }

    private List<String> generate(Random rnd, int n, int keyRange, int nullPct, int payloadMin, int payloadMax) {
        List<String> out = new ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            if (rnd.nextInt(100) < nullPct) {
                out.add(nullRow(rnd));
            } else {
                out.add(row(rnd.nextInt(keyRange), payloadMin + rnd.nextInt(payloadMax - payloadMin + 1), rnd));
            }
        }
        return out;
    }

    private List<byte[]> runJoin(Path left, Path right, long budget, Path tmp, TaskContext ctx)
            throws IOException, JoinCancelledException {
        SortResult ls = ExternalSorter.sort(left, "k", budget, tmp, "left", ctx);
        SortResult rs;
        try {
            rs = ExternalSorter.sort(right, "k", budget, tmp, "right", ctx);
            List<byte[]> got = new ArrayList<>();
            JoinEngine engine = new JoinEngine(budget, tmp, "eng");
            engine.join(ls.source, rs.source, got::add, ctx);
            return got;
        } finally {
            // nothing: sorter run files are caller-managed test fixtures under tmp
        }
    }

    /** Brute-force reference multiset: key -> rows, nested loop, nulls ignored. */
    private Map<String, Long> reference(List<String> left, List<String> right) {
        Map<Long, List<String>> lIndex = new HashMap<>();
        for (String line : left) {
            Map<String, Object> o = Json.parseObject(line);
            Object k = o.get("k");
            if (k instanceof Long) {
                lIndex.computeIfAbsent((Long) k, x -> new ArrayList<>()).add(line);
            }
        }
        Map<String, Long> expected = new HashMap<>();
        for (String r : right) {
            Map<String, Object> o = Json.parseObject(r);
            Object k = o.get("k");
            if (k instanceof Long) {
                List<String> ls = lIndex.get(k);
                if (ls != null) {
                    for (String l : ls) {
                        String out = "{\"left\":" + l + ",\"right\":" + r + "}";
                        expected.merge(out, 1L, Long::sum);
                    }
                }
            }
        }
        return expected;
    }

    private Map<String, Long> multiset(List<byte[]> got) {
        Map<String, Long> m = new HashMap<>();
        for (byte[] b : got) {
            String s = new String(b, StandardCharsets.UTF_8);
            // output lines are newline-terminated JSONL; compare on the JSON payload
            if (s.endsWith("\n")) {
                s = s.substring(0, s.length() - 1);
            }
            m.merge(s, 1L, Long::sum);
        }
        return m;
    }

    // ---------------------------------------------------------------- tests

    private void testBasicSemantics() throws Exception {
        Path dir = Files.createTempDirectory("join-basic-");
        // left:  1,1,2,3,null      right: 1,1,2,4,null
        // expected: 1: 2x2=4, 2: 1x1=1 -> 5 rows; nulls and 3/4 unmatched
        List<String> l = Arrays.asList(
                "{\"k\":1,\"v\":\"l1\"}", "{\"k\":1,\"v\":\"l2\"}",
                "{\"k\":2,\"v\":\"l3\"}", "{\"k\":3,\"v\":\"l4\"}",
                "{\"k\":null,\"v\":\"ln\"}");
        List<String> r = Arrays.asList(
                "{\"k\":1,\"v\":\"r1\"}", "{\"k\":1,\"v\":\"r2\"}",
                "{\"k\":2,\"v\":\"r3\"}", "{\"k\":4,\"v\":\"r4\"}",
                "{\"k\":null,\"v\":\"rn\"}");
        TableFile lt = writeTable(dir, "l.jsonl", l);
        TableFile rt = writeTable(dir, "r.jsonl", r);
        List<byte[]> got = runJoin(lt.file, rt.file, 1 << 20, dir, new TaskContext("t"));
        Map<String, Long> expected = reference(l, r);
        check(multiset(got).equals(expected), "basic semantics multiset (5 rows, cartesian, nulls dropped)");
        check(got.size() == 5, "basic semantics row count == 5 (got " + got.size() + ")");
        boolean noNulls = got.stream().noneMatch(b -> new String(b, UTF8).contains("\"v\":\"ln\"")
                || new String(b, UTF8).contains("\"v\":\"rn\""));
        check(noNulls, "NULL key rows never appear in output");
    }

    private void testMissingKeyIsNull() throws Exception {
        Path dir = Files.createTempDirectory("join-missing-");
        List<String> l = Arrays.asList("{\"k\":1,\"v\":\"a\"}", "{\"other\":5}");
        List<String> r = Collections.singletonList("{\"k\":1,\"v\":\"b\"}");
        TableFile lt = writeTable(dir, "l.jsonl", l);
        TableFile rt = writeTable(dir, "r.jsonl", r);
        List<byte[]> got = runJoin(lt.file, rt.file, 1 << 20, dir, new TaskContext("t"));
        check(got.size() == 1 && new String(got.get(0), UTF8).contains("\"v\":\"a\""),
                "missing key column behaves as NULL (1 match only)");
    }

    private void testRandomAgainstReference(long seed, int nl, int nr, long budget,
                                            boolean expectSpill, String label) throws Exception {
        Path dir = Files.createTempDirectory("join-rnd-" + label + "-");
        Random rnd = new Random(seed);
        List<String> l = generate(rnd, nl, 40, 10, 8, 40);
        List<String> r = generate(rnd, nr, 40, 10, 8, 40);
        TableFile lt = writeTable(dir, "l.jsonl", l);
        TableFile rt = writeTable(dir, "r.jsonl", r);
        TaskContext ctx = new TaskContext("t");
        SortResult ls = ExternalSorter.sort(lt.file, "k", budget, dir, "left", ctx);
        SortResult rs = ExternalSorter.sort(rt.file, "k", budget, dir, "right", ctx);
        List<byte[]> got = new ArrayList<>();
        new JoinEngine(budget, dir, "eng").join(ls.source, rs.source, got::add, ctx);
        Map<String, Long> expected = reference(l, r);
        check(multiset(got).equals(expected),
                label + ": output multiset equals brute-force reference (" + got.size() + " rows)");
        if (expectSpill) {
            check(ls.spilled && rs.spilled,
                    label + ": both tables spilled (runs L=" + ls.initialRuns + " R=" + rs.initialRuns + ")");
            check(ls.initialSpillBytes > 0 && rs.initialSpillBytes > 0,
                    label + ": spill byte counters positive");
            if (budget <= 128) {
                check(ls.mergePasses >= 1 || rs.mergePasses >= 1,
                        label + ": multi-pass merge engaged (passes L=" + ls.mergePasses
                                + " R=" + rs.mergePasses + ")");
            }
        } else {
            check(!ls.spilled && !rs.spilled, label + ": no spill for large budget");
        }
    }

    /**
     * Acceptance centerpiece: one hot key group larger than the memory budget on
     * BOTH sides, forcing group spills + block nested-loops join.
     */
    private void testHotKeyGroupLargerThanBudget() throws Exception {
        Path dir = Files.createTempDirectory("join-hotkey-");
        long budget = 400;
        Random rnd = new Random(2026);
        // 60 left rows and 50 right rows on key 7, each row ~80-100 bytes ->
        // each group (~5-9 KB) is far larger than the 400-byte budget.
        List<String> l = new ArrayList<>();
        List<String> r = new ArrayList<>();
        for (int i = 0; i < 60; i++) {
            String s = "{\"k\":7,\"i\":" + i + ",\"p\":\"" + pad(80, 'L', i) + "\"}";
            l.add(s);
        }
        for (int i = 0; i < 50; i++) {
            r.add("{\"k\":7,\"i\":" + i + ",\"p\":\"" + pad(80, 'R', i) + "\"}");
        }
        // sprinkle cold keys and nulls
        l.add("{\"k\":100,\"p\":\"cold-l\"}");
        r.add("{\"k\":101,\"p\":\"cold-r\"}");
        l.add("{\"k\":null,\"p\":\"n\"}");
        r.add("{\"k\":null,\"p\":\"n\"}");
        Collections.shuffle(l, rnd);
        Collections.shuffle(r, rnd);

        TableFile lt = writeTable(dir, "l.jsonl", l);
        TableFile rt = writeTable(dir, "r.jsonl", r);

        TaskContext ctx = new TaskContext("hot");
        SortResult ls = ExternalSorter.sort(lt.file, "k", budget, dir, "left", ctx);
        SortResult rs = ExternalSorter.sort(rt.file, "k", budget, dir, "right", ctx);
        List<byte[]> got = new ArrayList<>();
        JoinEngine engine = new JoinEngine(budget, dir, "hoteng");
        JoinEngine.Stats returned = engine.join(ls.source, rs.source, got::add, ctx);

        Map<String, Long> expected = reference(l, r);
        check(multiset(got).equals(expected), "hot-key: multiset exact (" + got.size() + " == 3000 rows)");
        check(got.size() == 3000, "hot-key: 60x50 cartesian = 3000");
        check(returned.groupSpillCount >= 2,
                "hot-key: both oversized groups spilled (groupSpillCount=" + returned.groupSpillCount + ")");
        check(returned.bnlBlockCount >= 1,
                "hot-key: block nested-loops path used (blocks=" + returned.bnlBlockCount + ")");
        check(ls.spilled || rs.spilled || returned.groupSpillCount >= 2,
                "hot-key: spill activity observed under 400-byte budget");

        // every per-group temp file must be deleted after the join
        long leftovers;
        try (var s = Files.list(dir)) {
            leftovers = s.filter(p -> p.getFileName().toString().contains("-grp")).count();
        }
        check(leftovers == 0, "hot-key: per-group spill files deleted afterwards");
    }

    private static String pad(int n, char c, int salt) {
        StringBuilder sb = new StringBuilder(n);
        for (int i = 0; i < n; i++) {
            sb.append((char) (c + ((i + salt) % 3)));
        }
        return sb.toString();
    }

    private void testTempCleanupOnSuccess() throws Exception {
        Path dir = Files.createTempDirectory("join-cleanup-");
        Random rnd = new Random(5);
        List<String> l = generate(rnd, 4000, 20, 5, 10, 30);
        List<String> r = generate(rnd, 4000, 20, 5, 10, 30);
        TableFile lt = writeTable(dir, "l.jsonl", l);
        TableFile rt = writeTable(dir, "r.jsonl", r);
        TaskContext ctx = new TaskContext("t");
        SortResult ls = ExternalSorter.sort(lt.file, "k", 1024, dir, "left", ctx);
        SortResult rs = ExternalSorter.sort(rt.file, "k", 1024, dir, "right", ctx);
        new JoinEngine(1024, dir, "eng").join(ls.source, rs.source, b -> {
        }, ctx);
        check(ls.spilled && rs.spilled, "cleanup: fixture forced spills");
        long filesLeft;
        try (var s = Files.list(dir)) {
            filesLeft = s.filter(p -> p.getFileName().toString().endsWith(".bin")).count();
        }
        // Sorter run files for engine-level tests live in dir and are owned by
        // the caller (JobService deletes them); GROUP spill files must be gone.
        long groupLeft;
        try (var s = Files.list(dir)) {
            groupLeft = s.filter(p -> p.getFileName().toString().contains("-grp")).count();
        }
        check(groupLeft == 0, "cleanup: no per-group files after successful join");
        check(filesLeft >= 1, "cleanup: sorter run files remain (engine-level call does not own them)");
    }

    private void testInvalidJsonReported() throws Exception {
        Path dir = Files.createTempDirectory("join-bad-");
        List<String> l = Arrays.asList("{\"k\":1}", "{not json");
        List<String> r = Collections.singletonList("{\"k\":1}");
        TableFile lt = writeTable(dir, "l.jsonl", l);
        TableFile rt = writeTable(dir, "r.jsonl", r);
        IOException caught = null;
        try {
            ExternalSorter.sort(lt.file, "k", 4096, dir, "left", new TaskContext("t"));
        } catch (IOException e) {
            caught = e;
        }
        check(caught != null && caught.getMessage().contains("line 2"),
                "invalid JSONL surfaces file + line number in error");

        // wrong key type
        Files.write(lt.file, Arrays.asList("{\"k\":\"oops\"}", "{\"k\":1}"), StandardCharsets.UTF_8);
        IOException caught2 = null;
        try {
            ExternalSorter.sort(lt.file, "k", 4096, dir, "left", new TaskContext("t"));
        } catch (IOException e) {
            caught2 = e;
        }
        check(caught2 != null && caught2.getMessage().contains("must be an integer or null"),
                "non-integer key type rejected with clear message");
    }

    private void testCancellationCleansTempFiles() throws Exception {
        Path data = Files.createTempDirectory("join-svc-data-");
        Files.createDirectories(data.resolve("inputs"));
        Random rnd = new Random(31337);
        // Large inputs + tiny budget => thousands of spills and repeated passes,
        // so the job stays RUNNING long enough to cancel it.
        List<String> l = generate(rnd, 120_000, 500, 5, 30, 60);
        List<String> r = generate(rnd, 120_000, 500, 5, 30, 60);
        Files.write(data.resolve("inputs/l.jsonl"), l, StandardCharsets.UTF_8);
        Files.write(data.resolve("inputs/r.jsonl"), r, StandardCharsets.UTF_8);

        JobService svc = new JobService(data, 1);
        String body = "{\"leftInput\":\"inputs/l.jsonl\",\"rightInput\":\"inputs/r.jsonl\","
                + "\"leftKeyColumn\":\"k\",\"rightKeyColumn\":\"k\",\"memoryBudgetBytes\":4096}";
        Job job;
        try (java.io.Reader br = new java.io.StringReader(body)) {
            job = svc.submit(br);
        }
        // let it start doing real work, then cancel
        Thread.sleep(150);
        boolean cancelAccepted = svc.cancel(job.id);
        Job terminal = awaitTerminal(svc, job.id, 15_000);
        check(cancelAccepted, "cancel accepted for running job");
        check(terminal != null && terminal.status() == Job.Status.CANCELLED,
                "job reached CANCELLED (was " + (terminal == null ? "null" : terminal.status()) + ")");
        check(terminal.error() != null && !terminal.error().isBlank(),
                "cancellation error message retained: " + (terminal == null ? "" : terminal.error()));
        check(waitForTempCleanup(terminal.tempDir, 10_000),
                "cancellation: per-job temp directory removed");
        check(!Files.exists(terminal.outputFile), "cancellation: partial result file deleted");
        // error stays queryable
        Job again = svc.get(job.id);
        check(again != null && again.error() != null, "cancelled job error still fetchable via get()");
        svc.shutdown();
    }

    private Job awaitTerminal(JobService svc, String id, long timeoutMs) throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            Job j = svc.get(id);
            if (j != null && j.isTerminal()) {
                return j;
            }
            Thread.sleep(25);
        }
        return svc.get(id);
    }

    /** The worker deletes the temp dir in its finally block after marking terminal. */
    private static boolean waitForTempCleanup(Path dir, long timeoutMs) throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            if (!Files.exists(dir)) {
                return true;
            }
            Thread.sleep(25);
        }
        return !Files.exists(dir);
    }

    private static final java.nio.charset.Charset UTF8 = StandardCharsets.UTF_8;
}
