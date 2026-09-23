package join;

import java.io.IOException;
import java.io.Reader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Future;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Stream;

/**
 * Owns the job registry, the worker pool and per-job filesystem lifecycle.
 *
 * Filesystem layout under the configured data directory:
 *
 *   data/
 *     _tmp/&lt;jobId&gt;/        run files and per-group spill files (always deleted when a job ends)
 *     results/&lt;jobId&gt;.jsonl join output (kept after success; deleted on failure/cancel)
 *
 * Input paths in requests must resolve to files under the data directory.
 */
public final class JobService {

    static final long DEFAULT_BUDGET = 64L * 1024 * 1024;
    static final long MAX_BUDGET = 4L * 1024 * 1024 * 1024; // 4 GiB request cap

    private final Path dataDir;
    private final Path tmpRoot;
    private final Path resultsDir;
    private final ThreadPoolExecutor pool;
    private final ConcurrentHashMap<String, Handle> jobs = new ConcurrentHashMap<>();

    public JobService(Path dataDir, int concurrency) throws IOException {
        this.dataDir = dataDir.toAbsolutePath().normalize();
        Files.createDirectories(this.dataDir);
        this.tmpRoot = Files.createDirectories(this.dataDir.resolve("_tmp"));
        this.resultsDir = Files.createDirectories(this.dataDir.resolve("results"));
        ThreadFactory tf = new ThreadFactory() {
            private final AtomicInteger n = new AtomicInteger();

            @Override
            public Thread newThread(Runnable r) {
                Thread t = new Thread(r, "join-worker-" + n.incrementAndGet());
                t.setDaemon(true);
                return t;
            }
        };
        this.pool = new ThreadPoolExecutor(concurrency, concurrency,
                0L, TimeUnit.MILLISECONDS, new LinkedBlockingQueue<>(), tf);
    }

    // ------------------------------------------------------------- submission

    @SuppressWarnings("unchecked")
    public Job submit(Reader requestBody) throws IOException {
        String text;
        try {
            text = readAll(requestBody);
        } catch (IOException e) {
            throw new BadRequestException("unreadable request body: " + e.getMessage());
        }
        Map<String, Object> body;
        try {
            body = Json.parseObject(text);
        } catch (IllegalArgumentException e) {
            throw new BadRequestException("request body must be a JSON object: " + e.getMessage());
        }
        Path left = requireInput(body, "leftInput");
        Path right = requireInput(body, "rightInput");
        String leftKey = requireKey(body, "leftKeyColumn");
        String rightKey = requireKey(body, "rightKeyColumn");
        long budget = requireBudget(body);

        JoinRequest req = new JoinRequest(left, right, leftKey, rightKey, budget);

        String id = TaskContext.newJobId();
        Path tempDir = tmpRoot.resolve(id);
        Files.createDirectories(tempDir);
        Path output = resultsDir.resolve(id + ".jsonl");
        Job job = new Job(id, req, tempDir, output);
        TaskContext ctx = new TaskContext(id);
        Handle handle = new Handle(job, ctx);
        jobs.put(id, handle);
        handle.future = pool.submit(() -> runJob(handle));
        return job;
    }

    private Path requireInput(Map<String, Object> body, String field) throws IOException {
        Object v = body.get(field);
        if (!(v instanceof String) || ((String) v).isBlank()) {
            throw new BadRequestException("'" + field + "' must be a non-empty file path string");
        }
        Path raw = Paths.get((String) v);
        Path resolved = raw.isAbsolute() ? raw.normalize() : dataDir.resolve(raw).normalize();
        Path canonicalData = dataDir.toRealPath();
        Path canonicalFile;
        try {
            canonicalFile = resolved.toRealPath();
        } catch (IOException e) {
            throw new BadRequestException("'" + field + "' does not exist: " + resolved);
        }
        if (!canonicalFile.startsWith(canonicalData)) {
            throw new BadRequestException(
                    "'" + field + "' must be inside the data directory " + canonicalData);
        }
        if (!Files.isRegularFile(canonicalFile)) {
            throw new BadRequestException("'" + field + "' is not a regular file: " + canonicalFile);
        }
        if (!Files.isReadable(canonicalFile)) {
            throw new BadRequestException("'" + field + "' is not readable: " + canonicalFile);
        }
        return canonicalFile;
    }

    private String requireKey(Map<String, Object> body, String field) {
        Object v = body.get(field);
        if (!(v instanceof String) || ((String) v).isBlank()) {
            throw new BadRequestException("'" + field + "' must be a non-empty string");
        }
        return (String) v;
    }

    private long requireBudget(Map<String, Object> body) {
        Object v = body.get("memoryBudgetBytes");
        if (v == null) {
            return DEFAULT_BUDGET;
        }
        long budget;
        if (v instanceof Long) {
            budget = (Long) v;
        } else if (v instanceof Number) {
            budget = ((Number) v).longValue();
        } else {
            throw new BadRequestException("'memoryBudgetBytes' must be an integer");
        }
        if (budget < 1) {
            throw new BadRequestException("'memoryBudgetBytes' must be >= 1");
        }
        if (budget > MAX_BUDGET) {
            throw new BadRequestException("'memoryBudgetBytes' must be <= " + MAX_BUDGET);
        }
        return budget;
    }

    // -------------------------------------------------------------- execution

    private void runJob(Handle handle) {
        Job job = handle.job;
        TaskContext ctx = handle.ctx;
        SortResult leftResult = null;
        SortResult rightResult = null;
        try {
            if (ctx.isCancelled()) {
                throw new JoinCancelledException("Job " + job.id + " was cancelled before it started");
            }
            job.markRunning();

            JoinRequest req = job.request;
            JoinEngine.Stats joinStats;
            long leftRows;
            long leftNulls;
            long rightRows;
            long rightNulls;

            leftResult = ExternalSorter.sort(req.leftInput, req.leftKeyColumn,
                    req.memoryBudgetBytes, job.tempDir, "left", ctx);
            try {
                rightResult = ExternalSorter.sort(req.rightInput, req.rightKeyColumn,
                        req.memoryBudgetBytes, job.tempDir, "right", ctx);
                try (JoinEngine.RowSinkAndClose sink = JoinEngine.closingFileSink(job.outputFile)) {
                    JoinEngine engine = new JoinEngine(req.memoryBudgetBytes, job.tempDir, job.id);
                    joinStats = engine.join(leftResult.source, rightResult.source, sink, ctx);
                }
            } finally {
                if (rightResult != null) {
                    rightResult.source.close();
                }
            }
            if (leftResult != null) {
                leftResult.source.close();
            }

            ctx.checkCancelled();

            long outBytes = Files.size(job.outputFile);
            job.markCompleted(joinStats.outputRows, outBytes,
                    sortSummary(leftResult, rightResult), joinSummary(joinStats));
        } catch (JoinCancelledException e) {
            cleanupOutput(job);
            job.markCancelled(e.getMessage());
        } catch (IOException | RuntimeException | Error e) {
            cleanupOutput(job);
            if (ctx.isCancelled()) {
                // future.cancel(true) can surface as InterruptedIOException etc.
                job.markCancelled("Job " + job.id + " cancelled during I/O: " + rootMessage(e));
            } else {
                String message = rootMessage(e);
                job.markFailed(e.getClass().getSimpleName(), message);
            }
        } finally {
            closeQuietly(leftResult);
            closeQuietly(rightResult);
            deleteTempDir(job.tempDir);
        }
    }

    private static void closeQuietly(SortResult r) {
        if (r == null) {
            return;
        }
        try {
            r.source.close();
        } catch (IOException | RuntimeException ignored) {
            // best effort
        }
    }

    private static void cleanupOutput(Job job) {
        try {
            Files.deleteIfExists(job.outputFile);
        } catch (IOException | RuntimeException ignored) {
            // best effort
        }
    }

    private static void deleteTempDir(Path dir) {
        if (!Files.exists(dir)) {
            return;
        }
        try (Stream<Path> walk = Files.walk(dir)) {
            walk.sorted(Comparator.reverseOrder()).forEach(p -> {
                try {
                    Files.deleteIfExists(p);
                } catch (IOException ignored) {
                    // best effort; a leftover file is reported via _tmp listing
                }
            });
        } catch (IOException | RuntimeException ignored) {
            // best effort
        }
    }

    private static Map<String, Object> sortSummary(SortResult l, SortResult r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("left", oneSort(l));
        m.put("right", oneSort(r));
        return m;
    }

    private static Map<String, Object> oneSort(SortResult r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("totalRows", r.totalRows);
        m.put("nullRowsDropped", r.nullRows);
        m.put("spilled", r.spilled);
        m.put("initialRunCount", r.initialRuns);
        m.put("mergePasses", r.mergePasses);
        m.put("initialSpillBytes", r.initialSpillBytes);
        m.put("totalTempBytesWritten", r.totalTempBytesWritten);
        return m;
    }

    private static Map<String, Object> joinSummary(JoinEngine.Stats s) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("groupSpillCount", s.groupSpillCount);
        m.put("groupSpillBytes", s.groupSpillBytes);
        m.put("blockNestedLoopBlockCount", s.bnlBlockCount);
        return m;
    }

    private static String rootMessage(Throwable t) {
        Throwable cur = t;
        while (cur.getCause() != null && cur.getCause() != cur) {
            cur = cur.getCause();
        }
        String name = t == cur ? "" : " (" + t.getClass().getSimpleName() + ")";
        return String.valueOf(cur.getMessage()) + name;
    }

    // --------------------------------------------------------------- queries

    public Job get(String id) {
        Handle h = jobs.get(id);
        return h == null ? null : h.job;
    }

    public List<Job> list() {
        return jobs.values().stream()
                .map(h -> h.job)
                .sorted(Comparator.comparing(j -> j.id))
                .collect(java.util.stream.Collectors.toList());
    }

    /**
     * Request cancellation. The flag is flipped immediately; the worker polls it
     * in its row loops. A still-queued job is also pulled from the executor
     * queue. Returns false if the job is already terminal.
     */
    public boolean cancel(String id) {
        Handle h = jobs.get(id);
        if (h == null) {
            return false;
        }
        Job job = h.job;
        if (job.isTerminal()) {
            return false;
        }
        h.ctx.cancel();
        if (h.future != null) {
            h.future.cancel(true);
        }
        // Belt-and-braces: a QUEUED task removed from the executor never runs,
        // so perform its terminal cleanup here. If the worker won the race and
        // already transitioned the job, markCancelled is a no-op there.
        if (job.status() == Job.Status.QUEUED) {
            if (job.casQueuedToCancelled("Job " + id + " was cancelled while queued")) {
                cleanupOutput(job);
                deleteTempDir(job.tempDir);
            }
        }
        return true;
    }

    public void shutdown() {
        pool.shutdownNow();
        try {
            pool.awaitTermination(5, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        // Safety net: remove anything left under _tmp from this process.
        deleteTempDir(tmpRoot);
    }

    private static String readAll(Reader r) throws IOException {
        StringBuilder sb = new StringBuilder();
        char[] buf = new char[8192];
        int n;
        while ((n = r.read(buf)) != -1) {
            sb.append(buf, 0, n);
        }
        return sb.toString();
    }

    /** 400-class errors with messages safe to return to clients. */
    public static final class BadRequestException extends RuntimeException {
        private static final long serialVersionUID = 1L;

        public BadRequestException(String message) {
            super(message);
        }
    }

    private static final class Handle {
        final Job job;
        final TaskContext ctx;
        volatile Future<?> future;

        Handle(Job job, TaskContext ctx) {
            this.job = job;
            this.ctx = ctx;
        }
    }
}
