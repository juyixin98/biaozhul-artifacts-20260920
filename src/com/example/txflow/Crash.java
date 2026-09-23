package com.example.txflow;

/**
 * 故障注入：用 {@link Runtime#halt} 模拟掉电 / kill -9。
 *
 * halt 与 System.exit 的区别：halt 不执行 shutdown hook、不运行 finalizer，
 * 进程立即终止——这是对“持久化协议崩溃窗口”最严格的模拟，
 * 任何只存在于内存或未落盘的内容都会丢失。
 */
public final class Crash {

    private Crash() {
    }

    public static void maybeHalt(Engine.FailPoint requested, Engine.FailPoint trigger, String label) {
        if (requested == trigger) {
            System.err.println("[CRASH-INJECTION] 触发故障点 " + label
                    + "：进程立即 halt(17)，不执行任何清理");
            System.err.flush();
            Runtime.getRuntime().halt(17);
        }
    }
}
