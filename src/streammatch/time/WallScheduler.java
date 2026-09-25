package streammatch.time;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

/**
 * 基于 {@link ScheduledExecutorService} 的墙钟调度器（服务层处理时间模式使用）。
 * 任务在独立单线程池中执行；引擎侧自身对状态加锁，因此任务与请求线程可安全并发。
 */
public final class WallScheduler implements TaskScheduler, AutoCloseable {

    private final ScheduledExecutorService executor;
    private final Clock clock;
    private final AtomicLong nextHandle = new AtomicLong(1);

    private record Entry(long handle, ScheduledFuture<?> future) {
    }

    private final java.util.Map<Long, Entry> tasks = new java.util.concurrent.ConcurrentHashMap<>();

    public WallScheduler(Clock clock, String threadName) {
        this.clock = clock;
        this.executor = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, threadName);
            t.setDaemon(true);
            return t;
        });
    }

    @Override
    public long scheduleAt(long fireAtMillis, Runnable task) {
        long delay = Math.max(0L, fireAtMillis - clock.now());
        long handle = nextHandle.getAndIncrement();
        Runnable wrapped = () -> {
            tasks.remove(handle);
            task.run();
        };
        ScheduledFuture<?> future = executor.schedule(wrapped, delay, TimeUnit.MILLISECONDS);
        tasks.put(handle, new Entry(handle, future));
        return handle;
    }

    @Override
    public void cancel(long handle) {
        Entry e = tasks.remove(handle);
        if (e != null) {
            e.future().cancel(false);
        }
    }

    @Override
    public void close() {
        executor.shutdownNow();
    }
}
