package streammatch.time;

/** 直接委托 {@link System#currentTimeMillis()} 的生产时钟。 */
public final class SystemClock implements Clock {

    public static final SystemClock INSTANCE = new SystemClock();

    private SystemClock() {
    }

    @Override
    public long now() {
        return System.currentTimeMillis();
    }
}
