package streamagg.core;

/** 真实墙钟时间。 */
public final class SystemClock implements Clock {
    @Override
    public long nowMillis() {
        return System.currentTimeMillis();
    }
}
