package com.example.tvl.engine;

import com.example.tvl.sql.DataType;

import java.util.ArrayList;
import java.util.List;

/**
 * 一个列批次：同一模式下的若干列，每列行数相同。
 * 空批次允许 rowCount=0（列数组仍按模式齐备）。
 */
public final class Batch {

    private final int rowCount;
    private final List<Column> columns;

    public Batch(int rowCount, List<Column> columns) {
        this.rowCount = rowCount;
        for (Column c : columns) {
            if (c.rowCount() != rowCount) {
                throw new SemanticException("批次列行数不一致：期望 " + rowCount + "，实际 " + c.rowCount());
            }
        }
        this.columns = List.copyOf(columns);
    }

    public int rowCount() {
        return rowCount;
    }

    public List<Column> columns() {
        return columns;
    }

    public Column column(int index) {
        return columns.get(index);
    }

    /**
     * 按模式把行式 JSON 数据转成列式批次。
     * rows 中每个元素是与模式等长的数组，元素为 Long/Double/String/null。
     */
    public static Batch fromRows(Schema schema, List<?> rows) {
        if (rows == null) {
            throw new SemanticException("批次缺少 rows");
        }
        int n = rows.size();
        List<Column> cols = new ArrayList<>(schema.size());
        for (int c = 0; c < schema.size(); c++) {
            DataType type = schema.columns().get(c).type();
            Column column = Column.fromRows(type, n, rows, c);
            cols.add(column);
        }
        return new Batch(n, cols);
    }
}
