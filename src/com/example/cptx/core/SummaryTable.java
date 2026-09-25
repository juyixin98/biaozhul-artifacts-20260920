package com.example.cptx.core;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 受控的本地汇总表（本项目唯一“外部副作用”落点）：
 *
 * <pre>
 *   summary.json = { "key": {"count":..,"sum":..}, ... }，tmp + 原子 rename 重写。
 * </pre>
 *
 * 幂等性关键：汇总表从不基于“增量”追加，而是在每次事务提交后用已提交事务日志
 * 完整重建（fold 全部已提交事务）。upsert 按 key 覆盖，因此重复应用同一事务
 * 不会产生重复计数——这正是“恢复后无重复提交”对受控本地汇总表成立的原因。
 *
 * 边界（见 README）：该保证依赖“汇总表可由本地提交日志确定性重建 + 按 key 幂等覆盖”。
 * 对任意外部副作用（发邮件、第三方扣款、不可重放的远程 API）不成立：
 * 提交与外部动作之间的崩溃窗口会造成重复，需要外部系统自身支持幂等键/事务回执。
 */
public final class SummaryTable {

    private final Path file;

    public SummaryTable(Path baseDir) throws IOException {
        Files.createDirectories(baseDir);
        this.file = baseDir.resolve("summary.json");
    }

    /** 由全部已提交事务重建汇总表（每个事务携带的是该检查点上聚合状态的完整快照行）。 */
    public Map<String, Map<String, Object>> rebuildFrom(List<OutputLog.Txn> txns) throws IOException {
        Map<String, Map<String, Object>> merged = new TreeMap<>();
        for (OutputLog.Txn txn : txns) {
            for (Map<String, Object> row : txn.rows) {
                String key = Json.str(row.get("key"));
                Map<String, Object> agg = new LinkedHashMap<>();
                agg.put("count", Json.lng(row.get("count")));
                agg.put("sum", Json.dbl(row.get("sum")));
                merged.put(key, agg); // 幂等覆盖：后提交的快照覆盖旧快照
            }
        }
        return merged;
    }

    /** 原子重写汇总表，并返回写入的视图。injectAfterRename=true 时重写完成后注入 TABLE_APPLY 故障。 */
    public Map<String, Map<String, Object>> rewriteAtomically(Map<String, Map<String, Object>> view,
                                                              boolean injectAfterRename,
                                                              long checkpointId) throws IOException {
        FileIO.writeAtomic(file, Json.pretty(new TreeMap<>(view)).getBytes(StandardCharsets.UTF_8));
        if (injectAfterRename) {
            // 此时故障无害：汇总表已经是最新的；即便不是，启动时也会由提交日志重建。
            throw new InjectedFaultException(FaultPhase.TABLE_APPLY, checkpointId);
        }
        return view;
    }

    /** 读取当前汇总表；文件不存在（尚未提交过）返回空表。 */
    public Map<String, Object> read() throws IOException {
        if (!Files.exists(file)) return new LinkedHashMap<>();
        return Json.obj(Json.parse(Files.readString(file, StandardCharsets.UTF_8)));
    }
}
