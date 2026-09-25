package com.example.cptx.core;

/**
 * 注入的模拟故障。测试/CLI 在指定检查点、指定阶段抛出本异常，
 * 模拟进程崩溃（命令行入口使用与 JVM 硬退出相同的退出码，之后以全新进程恢复）。
 */
public class InjectedFaultException extends RuntimeException {

    private static final long serialVersionUID = 1L;

    private final FaultPhase phase;
    private final long checkpointId;

    public InjectedFaultException(FaultPhase phase, long checkpointId) {
        super("注入故障：checkpointId=" + checkpointId + "，阶段=" + phase);
        this.phase = phase;
        this.checkpointId = checkpointId;
    }

    public FaultPhase phase() {
        return phase;
    }

    public long checkpointId() {
        return checkpointId;
    }
}
