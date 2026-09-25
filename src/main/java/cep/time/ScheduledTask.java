package cep.time;

/** 定时器句柄。 */
public interface ScheduledTask {
    long deadlineMillis();
    boolean isCancelled();
    void cancel();
}
