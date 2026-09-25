package drvb.time;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

/**
 * 生产用调度器：单个守护线程的 {@link ScheduledExecutorService}，按墙钟固定周期触发。
 *
 * <p>任务异常不会杀死周期（与 scheduleAtFixedRate 默认行为相反），
 * 异常被捕获并记录在句柄上供管理接口查看。
 */
public final class ExecutorScheduler implements Scheduler {

    private final ScheduledExecutorService executor;
    private final List<ExecutorTask> tasks = new ArrayList<>();

    public ExecutorScheduler() {
        ThreadFactory tf = new ThreadFactory() {
            private final AtomicInteger seq = new AtomicInteger();

            @Override
            public Thread newThread(Runnable r) {
                Thread t = new Thread(r, "drvb-scheduler-" + seq.incrementAndGet());
                t.setDaemon(true);
                return t;
            }
        };
        this.executor = Executors.newScheduledThreadPool(1, tf);
    }

    @Override
    public synchronized ScheduledTask scheduleFixedRate(String name, long periodMillis, Runnable task) {
        if (periodMillis <= 0) {
            throw new IllegalArgumentException("periodMillis 必须为正数");
        }
        ExecutorTask handle = new ExecutorTask(name, periodMillis);
        ScheduledFuture<?> future = executor.scheduleAtFixedRate(() -> {
            try {
                task.run();
                handle.runs.incrementAndGet();
            } catch (RuntimeException e) {
                handle.lastError = e;
            }
        }, periodMillis, periodMillis, TimeUnit.MILLISECONDS);
        handle.future = future;
        tasks.add(handle);
        return handle;
    }

    @Override
    public synchronized List<ScheduledTask> tasks() {
        return new ArrayList<>(tasks);
    }

    @Override
    public void close() {
        executor.shutdownNow();
    }

    static final class ExecutorTask implements ScheduledTask {
        final String name;
        final long periodMillis;
        final AtomicLong runs = new AtomicLong();
        volatile ScheduledFuture<?> future;
        volatile RuntimeException lastError;

        ExecutorTask(String name, long periodMillis) {
            this.name = name;
            this.periodMillis = periodMillis;
        }

        @Override
        public String name() {
            return name;
        }

        @Override
        public long periodMillis() {
            return periodMillis;
        }

        @Override
        public void cancel() {
            if (future != null) {
                future.cancel(false);
            }
        }

        @Override
        public long runCount() {
            return runs.get();
        }
    }
}
