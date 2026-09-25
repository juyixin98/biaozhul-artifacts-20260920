package cep.time;

/**
 * 可手动推进的虚拟时钟：测试把它与 {@link HeapScheduler} 一起注入，
 * 即可在不依赖墙钟、不 sleep 的情况下验证"时间推进 → 定时器触发"。
 */
public final class VirtualClock implements Clock {

    private long now;

    public VirtualClock() { this(0L); }

    public VirtualClock(long start) { this.now = start; }

    @Override public long nowMillis() { return now; }

    public void setTime(long t) { this.now = t; }

    public void advance(long deltaMillis) {
        if (deltaMillis < 0) {
            throw new IllegalArgumentException("delta 必须 >= 0");
        }
        this.now += deltaMillis;
    }
}
