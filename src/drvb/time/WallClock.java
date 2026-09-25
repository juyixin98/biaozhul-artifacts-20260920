package drvb.time;

/** 墙钟实现，直接读取系统时间。生产模式使用。 */
public final class WallClock implements Clock {
    @Override
    public long nowMillis() {
        return System.currentTimeMillis();
    }
}
