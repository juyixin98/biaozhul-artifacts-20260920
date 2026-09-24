package com.tvl.test;

import com.tvl.columnar.Batch;
import com.tvl.columnar.ColumnVector;
import com.tvl.columnar.Schema;
import com.tvl.types.DataType;

import java.util.ArrayList;
import java.util.List;

/** 测试用的批次小工具：按"行"方便地构造列批次（每列持有独立的值数组）。 */
final class Batches {

    private Batches() {
    }

    /** 行 -> 列。rows 中每个元素是 Object[]，null 元素代表 SQL NULL。 */
    static Batch fromRows(Schema schema, List<Object[]> rows) {
        int n = rows.size();
        int cols = schema.size();

        // 每列独立的值数组与 NULL 位图（不能跨列共享，否则同类型列会互相覆盖）
        long[][] longs = new long[cols][n];
        double[][] doubles = new double[cols][n];
        String[][] strings = new String[cols][n];
        Boolean[][] booleans = new Boolean[cols][n];
        boolean[][] nulls = new boolean[cols][n];

        for (int r = 0; r < n; r++) {
            Object[] row = rows.get(r);
            for (int c = 0; c < cols; c++) {
                String name = schema.names().get(c);
                DataType type = schema.typeOf(name);
                Object v = c < row.length ? row[c] : null;
                if (v == null) {
                    nulls[c][r] = true;
                    continue;
                }
                switch (type) {
                    case INTEGER -> longs[c][r] = ((Number) v).longValue();
                    case DOUBLE -> doubles[c][r] = ((Number) v).doubleValue();
                    case STRING -> strings[c][r] = (String) v;
                    case BOOLEAN -> booleans[c][r] = (Boolean) v;
                }
            }
        }

        List<ColumnVector> columns = new ArrayList<>(cols);
        for (int c = 0; c < cols; c++) {
            DataType type = schema.typeOf(schema.names().get(c));
            columns.add(switch (type) {
                case INTEGER -> new ColumnVector(DataType.INTEGER, longs[c], nulls[c]);
                case DOUBLE -> new ColumnVector(DataType.DOUBLE, doubles[c], nulls[c]);
                case STRING -> new ColumnVector(DataType.STRING, strings[c], nulls[c]);
                case BOOLEAN -> new ColumnVector(DataType.BOOLEAN, booleans[c], nulls[c]);
            });
        }
        return new Batch(schema, columns);
    }
}
