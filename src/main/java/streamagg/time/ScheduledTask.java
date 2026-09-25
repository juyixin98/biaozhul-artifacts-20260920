package streamagg.time;

/** Handle to a scheduled task so callers can cancel it. */
public interface ScheduledTask {
    void cancel();
}
