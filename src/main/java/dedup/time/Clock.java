package dedup.time;

/**
 * 可注入的时钟。业务去重判定只使用事件自带的 eventTime，不读时钟；
 * 时钟仅用于“周期性水位线推进”这样的调度场景，因此可在测试中完全确定性地替换。
 */
public interface Clock {

    /** 当前时间（epoch 毫秒）。 */
    long nowMillis();

    /** 系统墙钟（生产默认）。 */
    static Clock system() {
        return System::currentTimeMillis;
    }

    /** 手动时钟（测试用），初始时间可指定，{@link #advance} 推动。 */
    static ManualClock manual(long initial) {
        return new ManualClock(initial);
    }

    final class ManualClock implements Clock {
        private long now;

        ManualClock(long now) { this.now = now; }

        @Override
        public long nowMillis() { return now; }

        /** 推进时间（负数会抛出异常 —— 时钟不能手动往回拨）。 */
        public void advance(long deltaMillis) {
            if (deltaMillis < 0) {
                throw new IllegalArgumentException("手动时钟只能向前推进");
            }
            now += deltaMillis;
        }

        public void setMillis(long t) {
            if (t < now) {
                throw new IllegalArgumentException("手动时钟不能回拨: " + t + " < " + now);
            }
            now = t;
        }
    }
}
