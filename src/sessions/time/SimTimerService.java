package sessions.time;

import java.util.PriorityQueue;

/**
 * 确定性的事件时间定时器模拟实现：不接触墙上时钟，完全由
 * {@link #advanceWatermark(long)} 驱动。同一边界类型、同一时刻的回调按注册先后触发。
 */
public final class SimTimerService implements TimerService {

    private static final class Timer implements Comparable<Timer> {
        final long fireTime;
        final boolean strictAfter; // true: 仅当 W > fireTime；false: W >= fireTime
        final long sequence;
        final Runnable callback;

        Timer(long fireTime, boolean strictAfter, long sequence, Runnable callback) {
            this.fireTime = fireTime;
            this.strictAfter = strictAfter;
            this.sequence = sequence;
            this.callback = callback;
        }

        boolean dueAt(long watermark, boolean infinity) {
            if (infinity) {
                return true;
            }
            return strictAfter ? fireTime < watermark : fireTime <= watermark;
        }

        @Override
        public int compareTo(Timer o) {
            int c = Long.compare(fireTime, o.fireTime);
            if (c != 0) {
                return c;
            }
            // 同刻：闭区间（封窗）先于严格（清除），再按注册顺序
            c = Boolean.compare(strictAfter, o.strictAfter);
            return c != 0 ? c : Long.compare(sequence, o.sequence);
        }
    }

    private final PriorityQueue<Timer> timers = new PriorityQueue<>();
    private long watermark = Long.MIN_VALUE;
    private long sequence = 0;

    @Override
    public long currentWatermark() {
        return watermark;
    }

    @Override
    public void registerTimer(long fireTime, Runnable callback) {
        timers.offer(new Timer(fireTime, false, sequence++, callback));
    }

    @Override
    public void registerTimerAfter(long fireTime, Runnable callback) {
        timers.offer(new Timer(fireTime, true, sequence++, callback));
    }

    @Override
    public void advanceWatermark(long newWatermark) {
        if (newWatermark < watermark) {
            return; // 水位线单调不减
        }
        watermark = newWatermark;
        boolean infinity = newWatermark == Long.MAX_VALUE;
        // 回调可能注册新定时器，因此循环到队首不再到点为止。
        while (!timers.isEmpty()) {
            Timer next = timers.peek();
            if (!next.dueAt(watermark, infinity)) {
                break;
            }
            timers.poll().callback.run();
        }
    }
}
