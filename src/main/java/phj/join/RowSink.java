package phj.join;

import phj.core.Row;

/** 结果行接收器。默认收集到内存列表；也可流式消费避免超大结果集撑爆内存。 */
@FunctionalInterface
public interface RowSink {
    void accept(Row row);

    /** 收集到内存 List 的默认实现。 */
    static RowSink collecting(java.util.List<Row> out) {
        return out::add;
    }
}
