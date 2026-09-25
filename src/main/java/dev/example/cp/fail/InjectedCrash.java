package dev.example.cp.fail;

/**
 * 注入的“崩溃”。
 *
 * <p>进程内测试使用 {@code halt=false}：抛出本异常模拟崩溃，进程不死，文件留在崩溃现场，
 * 可以紧接着用“重新打开引擎”来模拟恢复。
 *
 * <p>CLI 真实进程测试使用 {@code halt=true}：调用 {@link Runtime#halt(int)} 立即终止 JVM
 * （不运行 shutdown hook、不 flush），最接近 kill -9 / 断电。
 */
public final class InjectedCrash extends Error {

    private final CrashPoint point;
    private final long epochId;

    public InjectedCrash(CrashPoint point, long epochId) {
        super("injected crash at " + point + " during epoch " + epochId);
        this.point = point;
        this.epochId = epochId;
    }

    public CrashPoint point() {
        return point;
    }

    public long epochId() {
        return epochId;
    }
}
