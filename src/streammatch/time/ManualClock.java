package streammatch.time;

/**
 * 由测试/驱动程序显式拨动的时钟。{@link #advanceTo(long)} 只前进不回退。
 */
public final class ManualClock implements Clock {

    private long now;

    public ManualClock(long start) {
        this.now = start;
    }

    @Override
    public long now() {
        return now;
    }

    public void setTime(long t) {
        if (t < now) {
            throw new IllegalArgumentException("clock cannot move backwards: " + t + " < " + now);
        }
        this.now = t;
    }

    public void advance(long deltaMillis) {
        if (deltaMillis < 0) {
            throw new IllegalArgumentException("delta must be >= 0");
        }
        this.now += deltaMillis;
    }

    public void advanceTo(long t) {
        setTime(t);
    }
}
