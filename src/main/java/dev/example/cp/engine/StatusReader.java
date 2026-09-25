package dev.example.cp.engine;

import dev.example.cp.storage.OutputStore;
import dev.example.cp.storage.StateStore;
import dev.example.cp.storage.SummaryTable;
import dev.example.cp.fail.FailureInjector;

import java.nio.file.Path;

/**
 * 只读观察器：<b>不执行任何恢复或写入</b>，直接读取磁盘上的当前现场。
 * 供测试在“崩溃后、恢复前”断言中间不变量使用。
 */
public final class StatusReader {

    private final StateStore states;
    private final OutputStore outputs;
    private final SummaryTable table;

    public StatusReader(Path dataDir) {
        FailureInjector faults = new FailureInjector(dataDir);
        this.states = new StateStore(dataDir, faults);
        this.outputs = new OutputStore(dataDir, faults);
        this.table = new SummaryTable(dataDir, faults);
    }

    /** 读取已发布状态的 epoch（无检查点返回 0），不修改任何文件。 */
    public long stateEpochOrZero() {
        CheckpointState cp = states.loadLatest();
        return cp != null ? cp.epochId() : 0L;
    }

    public SummaryTable.TableState table() {
        return table.load();
    }
}
