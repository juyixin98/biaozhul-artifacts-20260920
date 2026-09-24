package com.tvl.columnar;

import java.util.Arrays;
import java.util.Collections;
import java.util.List;

/**
 * 一个列批次：共享的模式 + 等长的列向量。
 * 允许 0 行的空批次（schema 仍非空），用于覆盖空批次语义。
 */
public final class Batch {

    private final Schema schema;
    private final List<ColumnVector> columns;

    public Batch(Schema schema, List<ColumnVector> columns) {
        if (schema.size() != columns.size()) {
            throw new IllegalArgumentException(
                    "schema 列数 " + schema.size() + " 与实际列数 " + columns.size() + " 不一致");
        }
        int rows = columns.isEmpty() ? 0 : columns.get(0).size();
        for (int i = 0; i < columns.size(); i++) {
            ColumnVector col = columns.get(i);
            String name = schema.names().get(i);
            if (col.type() != schema.typeOf(name)) {
                throw new IllegalArgumentException("列 " + name + " 实际类型 "
                        + col.type() + " 与 schema " + schema.typeOf(name) + " 不符");
            }
            if (col.size() != rows) {
                throw new IllegalArgumentException("列 " + name + " 行数 "
                        + col.size() + " 与批次行数 " + rows + " 不一致");
            }
        }
        this.schema = schema;
        this.columns = columns;
    }

    public static Batch of(Schema schema, ColumnVector... columns) {
        return new Batch(schema, Arrays.asList(columns));
    }

    public Schema schema() {
        return schema;
    }

    public List<ColumnVector> columns() {
        return Collections.unmodifiableList(columns);
    }

    public ColumnVector column(int index) {
        return columns.get(index);
    }

    public ColumnVector column(String name) {
        return columns.get(schema.indexOf(name));
    }

    public int rowCount() {
        return columns.isEmpty() ? 0 : columns.get(0).size();
    }
}
