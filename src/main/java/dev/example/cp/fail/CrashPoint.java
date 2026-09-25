package dev.example.cp.fail;

/**
 * 检查点 / 输出事务各阶段的故障注入点。
 *
 * <pre>
 *  barrier 到达后，对 epochId 的处理顺序：
 *    1. writePending()  —— 将本 epoch 输出写入 output/committing/&lt;id&gt;.pending
 *    2. STATE_WRITE    —— 将算子状态+偏移写入 state/checkpoint-&lt;id&gt;.tmp 并原子发布 latest
 *    3. COMMIT_RENAME  —— pending 文件原子改名为 committed
 *    4. TABLE_APPLY    —— 把本 epoch 的聚合结果应用进受控汇总表（原子重写 table.json）
 *    5. AFTER_COMMIT   —— 全部持久化之后（模拟“输出提交之后、进程继续运行前”的崩溃）
 * </pre>
 */
public enum CrashPoint {

    /** 状态写入点：operator state + input offset 落盘的过程中。 */
    STATE_WRITE,

    /** 输出提交点（rename）：pending→committed 的原子改名“之前”。 */
    COMMIT_RENAME,

    /** 输出提交点（应用表）：committed 已可见，但汇总表尚未原子重写。 */
    TABLE_APPLY,

    /** 全部提交完成之后崩溃（用于验证提交后的崩溃不产生重复）。 */
    AFTER_COMMIT,

    /** 输出准备阶段：pending 输出日志写入中途（另一个状态前的点）。 */
    PENDING_WRITE;

    public static CrashPoint parse(String s) {
        return valueOf(s.trim().toUpperCase().replace('-', '_'));
    }
}
