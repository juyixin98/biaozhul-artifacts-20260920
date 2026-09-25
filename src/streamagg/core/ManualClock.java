package streamagg.core;

import java.util.concurrent.atomic.AtomicLong;

/**
 * 手动时钟：时间只能通过 {@link #advance(long)} / {@link #set(long)} 推进，
 * 供确定性测试使用。初始时间固定为 {@code 1_700_000_000_000L}（2023-11-14）。
 */
public final class ManualClock implements Clock {
    private final AtomicLong current;

    public ManualClock() {
        this(1_700_000_000_000L);
    }

    public ManualClock(long startMillis) {
        this.current = new AtomicLong(startMillis);
    }

    @Override
    public long nowMillis() {
        return current.get();
    }

    public void advance(long deltaMillis) {
        if (deltaMillis < 0) {
            throw new IllegalArgumentException("时间不允许倒流: " + deltaMillis);
        }
        current.addAndGet(deltaMillis);
    }

    public void set(long millis) {
        current.set(millis);
    }
}
