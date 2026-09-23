package windowengine.engine;

import windowengine.Row;
import windowengine.Schema;
import windowengine.Value;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 分区器：按 PARTITION BY 键把行划分为若干分区。
 *
 * 分区相等规则与 SQL 一致：同列上 NULL = NULL（NULL 值会分到同一组），
 * LONG 与 STRING 互不相等（不报错——分区等值不需要跨类型排序）。
 * 分区内的行保留输入顺序；分区本身按首次出现顺序排列（仅影响导出可读性）。
 */
public final class Partitioner {

    private final int[] columns;

    public Partitioner(Schema schema, List<String> partitionColumns) {
        this.columns = new int[partitionColumns.size()];
        for (int i = 0; i < columns.length; i++) {
            columns[i] = schema.requireIndex(partitionColumns.get(i));
        }
    }

    public List<List<Row>> partition(List<Row> rows) {
        if (columns.length == 0) {
            // 无 PARTITION BY：所有行（含空表）属于唯一分区
            return List.of(new ArrayList<>(rows));
        }
        Map<List<Value>, List<Row>> groups = new LinkedHashMap<>();
        for (Row row : rows) {
            List<Value> key = new ArrayList<>(columns.length);
            for (int c : columns) {
                key.add(row.get(c));
            }
            groups.computeIfAbsent(key, k -> new ArrayList<>()).add(row);
        }
        return new ArrayList<>(groups.values());
    }
}
