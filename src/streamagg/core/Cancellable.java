package streamagg.core;

/** 可取消的定时任务句柄。 */
public interface Cancellable {
    void cancel();
}
