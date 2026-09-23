package join;

import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Server-side state of one join request.
 *
 * Status transitions: QUEUED -> RUNNING -> COMPLETED | FAILED | CANCELLED.
 * Error text is retained after FAILED/CANCELLED so clients can fetch it with
 * GET /join/{id}; the per-job temp directory is deleted in every terminal state.
 */
public final class Job {

    public enum Status {QUEUED, RUNNING, COMPLETED, FAILED, CANCELLED}

    public final String id;
    public final JoinRequest request;
    public final Path tempDir;
    public final Path outputFile;
    public final long createdAtMs = System.currentTimeMillis();

    private volatile Status status = Status.QUEUED;
    private volatile String error;
    private volatile String errorType;
    private volatile long startedAtMs;
    private volatile long finishedAtMs;

    private volatile Map<String, Object> sortInfo = new LinkedHashMap<>();
    private volatile Map<String, Object> joinInfo = new LinkedHashMap<>();
    private volatile long outputRows;
    private volatile long outputBytes;

    Job(String id, JoinRequest request, Path tempDir, Path outputFile) {
        this.id = id;
        this.request = request;
        this.tempDir = tempDir;
        this.outputFile = outputFile;
    }

    public Status status() {
        return status;
    }

    public String error() {
        return error;
    }

    public long outputRows() {
        return outputRows;
    }

    void markRunning() {
        synchronized (this) {
            if (status == Status.QUEUED) {
                status = Status.RUNNING;
                startedAtMs = System.currentTimeMillis();
            }
        }
    }

    /** Atomic QUEUED -> CANCELLED for a task that never started. */
    synchronized boolean casQueuedToCancelled(String message) {
        if (status != Status.QUEUED) {
            return false;
        }
        markCancelled(message);
        return true;
    }

    void markCompleted(long rows, long bytes, Map<String, Object> sort, Map<String, Object> join) {
        status = Status.COMPLETED;
        finishedAtMs = System.currentTimeMillis();
        outputRows = rows;
        outputBytes = bytes;
        sortInfo = sort;
        joinInfo = join;
    }

    void markFailed(String type, String message) {
        status = Status.FAILED;
        finishedAtMs = System.currentTimeMillis();
        errorType = type;
        error = message;
    }

    void markCancelled(String message) {
        status = Status.CANCELLED;
        finishedAtMs = System.currentTimeMillis();
        errorType = "cancelled";
        error = message;
    }

    public boolean isTerminal() {
        switch (status) {
            case COMPLETED:
            case FAILED:
            case CANCELLED:
                return true;
            default:
                return false;
        }
    }

    public Map<String, Object> describe() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", id);
        m.put("status", status.name());
        m.put("createdAtMs", createdAtMs);
        if (startedAtMs != 0) {
            m.put("startedAtMs", startedAtMs);
        }
        if (finishedAtMs != 0) {
            m.put("finishedAtMs", finishedAtMs);
            m.put("elapsedMs", finishedAtMs - startedAtMs);
        }
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("leftKey", request.leftKeyColumn);
        req.put("rightKey", request.rightKeyColumn);
        req.put("memoryBudgetBytes", request.memoryBudgetBytes);
        m.put("request", req);
        if (error != null) {
            Map<String, Object> e = new LinkedHashMap<>();
            e.put("type", errorType);
            e.put("message", error);
            m.put("error", e);
        }
        if (status == Status.COMPLETED) {
            m.put("outputRows", outputRows);
            m.put("outputBytes", outputBytes);
            m.put("resultFile", outputFile.toString());
            m.put("resultUrl", "/join/" + id + "/result");
            m.put("sort", sortInfo);
            m.put("join", joinInfo);
        }
        return m;
    }
}
