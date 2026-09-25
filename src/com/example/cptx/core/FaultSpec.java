package com.example.cptx.core;

import java.util.EnumMap;
import java.util.Map;

/**
 * 故障注入规格：每个阶段记录“下一个要命中的检查点 ID”。
 * 一次性触发：命中检查（isArmed）即被消耗；同一阶段可再次 arm 实现连续两次崩溃。
 * 记录值 &lt;= 0 表示“下一次检查点必中”。
 */
public final class FaultSpec {

    private final Map<FaultPhase, Long> arms = new EnumMap<>(FaultPhase.class);

    /** 在第 checkpointId 次检查点的指定阶段注入故障；checkpointId &lt;= 0 表示下一次即中。 */
    public FaultSpec arm(FaultPhase phase, long checkpointId) {
        arms.put(phase, checkpointId);
        return this;
    }

    public FaultSpec armNext(FaultPhase phase) {
        return arm(phase, 0L);
    }

    /**
     * 查询本次检查点是否应在该阶段崩溃。命中即一次性消耗（移除该 arm），
     * 由存储层在“恰好的注入点”抛出 {@link InjectedFaultException}。
     */
    public boolean isArmed(FaultPhase phase, long checkpointId) {
        Long at = arms.get(phase);
        if (at == null) return false;
        if (at <= 0L || at == checkpointId) {
            arms.remove(phase);
            return true;
        }
        return false;
    }

    public boolean anyArmed() {
        return !arms.isEmpty();
    }

    public Map<FaultPhase, Long> arms() {
        return new EnumMap<>(arms);
    }
}
