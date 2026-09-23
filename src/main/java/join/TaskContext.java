package join;

import java.util.concurrent.atomic.AtomicLong;

/**
 * Cancellation handle shared by the HTTP handler (which flips the flag) and the
 * join worker (which polls {@link #checkCancelled()} in its row loops).
 */
public final class TaskContext {

    private static final AtomicLong SEQ = new AtomicLong();

    public final String jobId;
    private volatile boolean cancelled;

    public TaskContext(String jobId) {
        this.jobId = jobId;
    }

    public static String newJobId() {
        return "job-" + System.currentTimeMillis() + "-" + SEQ.incrementAndGet();
    }

    public void cancel() {
        cancelled = true;
    }

    public boolean isCancelled() {
        return cancelled;
    }

    public void checkCancelled() throws JoinCancelledException {
        if (cancelled) {
            throw new JoinCancelledException("Job " + jobId + " was cancelled by the client");
        }
    }
}
