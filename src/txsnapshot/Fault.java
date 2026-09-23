package txsnapshot;

/**
 * 一次性故障注入点。arm 之后，下一次命中指定阶段时按模式处置：
 * - HALT：Runtime.halt(0)，模拟进程被杀（不跑 shutdown hook，最接近断电/kill -9）
 * - EXCEPTION：抛 {@link InjectedFault}，模拟异常崩溃（进程继续存活，用于进程内测试）
 *
 * 一次只武装一个点、触发一次，随后自动解除，保证故障边界确定。
 */
public final class Fault {

    public enum Point {
        DURING_PROCESSING,  // 状态计算之后、任何落盘之前
        AFTER_STATE_PERSISTED, // 快照原子发布之后、commit 标记之前
        DURING_COMMIT,      // commit 标记写到一半
        DURING_PREPARE      // 输出 prepare 写到一半
    }

    public enum Mode { HALT, EXCEPTION }

    static final class InjectedFault extends RuntimeException {
        final Point point;
        InjectedFault(Point point) {
            super("injected fault at " + point);
            this.point = point;
        }
    }

    private volatile Point armedPoint;
    private volatile Mode armedMode;
    private volatile long armedOffset = -1; // -1 表示任意偏移都触发

    public synchronized void arm(Point point, Mode mode, long offset) {
        this.armedPoint = point;
        this.armedMode = mode;
        this.armedOffset = offset;
    }

    public synchronized void disarm() {
        this.armedPoint = null;
    }

    /**
     * @return true 表示故障在此触发（HALT 模式下不会返回，进程直接退出）；
     *         EXCEPTION 模式抛异常；未武装返回 false
     */
    public boolean fire(Point point, long offset) {
        Point p;
        Mode m;
        long want;
        synchronized (this) {
            if (armedPoint != point) return false;
            if (armedOffset >= 0 && armedOffset != offset) return false;
            p = armedPoint;
            m = armedMode;
            armedPoint = null;
        }
        System.err.println("[fault] injected at " + p + " for txn offset=" + offset
                + " mode=" + m);
        System.err.flush();
        if (m == Mode.HALT) {
            Runtime.getRuntime().halt(0);
        }
        throw new InjectedFault(p);
    }
}
