package com.tvl.http;

import com.tvl.columnar.Batch;
import com.tvl.columnar.ColumnVector;
import com.tvl.columnar.Schema;
import com.tvl.engine.BatchResult;
import com.tvl.engine.QueryRequest;
import com.tvl.json.JsonException;
import com.tvl.types.DataType;
import com.tvl.types.TypeCheckException;
import com.tvl.types.Values;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * JSON 请求解码 / 结果编码。
 *
 * 请求（POST /query）：
 * {
 *   "sql": "SELECT ...",
 *   "paramTypes": ["INTEGER", "STRING"],
 *   "params": [10, "x"],
 *   "batches": [ { "schema": [{"name":"a","type":"INTEGER"}],
 *                  "rows": [{"a":1}, {"a":null}] } ],
 *   "crossCheck": true
 * }
 */
public final class ApiCodec {

    private ApiCodec() {
    }

    public static QueryRequest decode(Object body) {
        if (!(body instanceof Map<?, ?> root)) {
            throw new JsonException("请求体必须是 JSON 对象");
        }
        String sql = requireString(root, "sql");
        List<DataType> paramTypes = new ArrayList<>();
        for (Object t : asArray(root.get("paramTypes"), "paramTypes")) {
            if (!(t instanceof String s)) {
                throw new JsonException("paramTypes 必须是类型名字符串数组");
            }
            try {
                paramTypes.add(DataType.parse(s));
            } catch (IllegalArgumentException e) {
                throw new TypeCheckException(e.getMessage());
            }
        }
        List<Object> params = new ArrayList<>(asArray(root.get("params"), "params"));
        boolean crossCheck = Boolean.TRUE.equals(root.get("crossCheck"));

        List<Batch> batches = new ArrayList<>();
        for (Object b : asArray(root.get("batches"), "batches")) {
            batches.add(decodeBatch(b));
        }
        return new QueryRequest(sql, paramTypes, params, batches, crossCheck);
    }

    private static Batch decodeBatch(Object raw) {
        if (!(raw instanceof Map<?, ?> bm)) {
            throw new JsonException("batches 的每个元素必须是对象");
        }
        List<String> names = new ArrayList<>();
        Map<String, DataType> typeMap = new LinkedHashMap<>();
        for (Object col : asArray(bm.get("schema"), "schema")) {
            if (!(col instanceof Map<?, ?> cm)) {
                throw new JsonException("schema 元素必须是 {name,type} 对象");
            }
            Object name = cm.get("name");
            Object type = cm.get("type");
            if (!(name instanceof String n) || !(type instanceof String t)) {
                throw new JsonException("schema 元素需要字符串字段 name 与 type");
            }
            if (typeMap.put(n, DataType.parse(t)) != null || names.contains(n)) {
                throw new JsonException("schema 中列名重复: " + n);
            }
            names.add(n);
        }
        if (names.isEmpty()) {
            throw new JsonException("schema 至少需要一列");
        }
        Schema schema = new Schema(names, typeMap);

        List<?> rows = asArray(bm.get("rows"), "rows");

        // 每列独立的值数组与 NULL 位图（不能跨列共享，否则同类型列会互相覆盖）
        int n = rows.size();
        long[][] longs = new long[names.size()][n];
        double[][] doubles = new double[names.size()][n];
        String[][] strings = new String[names.size()][n];
        Boolean[][] booleans = new Boolean[names.size()][n];
        boolean[][] nulls = new boolean[names.size()][n];

        for (int r = 0; r < n; r++) {
            Object rowObj = rows.get(r);
            if (!(rowObj instanceof Map<?, ?> row)) {
                throw new JsonException("第 " + r + " 行必须是对象");
            }
            for (int c = 0; c < names.size(); c++) {
                String name = names.get(c);
                DataType type = typeMap.get(name);
                if (!row.containsKey(name)) {
                    nulls[c][r] = true;
                    continue;
                }
                Object value;
                try {
                    value = Values.coerce(type, row.get(name),
                            "列 '" + name + "' 第 " + r + " 行");
                } catch (TypeCheckException e) {
                    throw e;
                }
                if (value == null) {
                    nulls[c][r] = true;
                    continue;
                }
                switch (type) {
                    case INTEGER -> longs[c][r] = (Long) value;
                    case DOUBLE -> doubles[c][r] = (Double) value;
                    case STRING -> strings[c][r] = (String) value;
                    case BOOLEAN -> booleans[c][r] = (Boolean) value;
                }
            }
        }

        List<ColumnVector> columns = new ArrayList<>(names.size());
        for (int c = 0; c < names.size(); c++) {
            DataType type = typeMap.get(names.get(c));
            switch (type) {
                case INTEGER -> columns.add(new ColumnVector(DataType.INTEGER, longs[c], nulls[c]));
                case DOUBLE -> columns.add(new ColumnVector(DataType.DOUBLE, doubles[c], nulls[c]));
                case STRING -> columns.add(new ColumnVector(DataType.STRING, strings[c], nulls[c]));
                case BOOLEAN -> columns.add(new ColumnVector(DataType.BOOLEAN, booleans[c], nulls[c]));
            }
        }
        return new Batch(schema, columns);
    }

    public static Map<String, Object> encodeResults(List<BatchResult> results,
                                                     List<String> outputNames,
                                                     List<DataType> outputTypes) {
        List<Object> batchJson = new ArrayList<>(results.size());
        long totalSelected = 0;
        for (BatchResult r : results) {
            Map<String, Object> bj = new LinkedHashMap<>();
            bj.put("inputRows", r.inputRows());
            bj.put("truth", encodeTruth(r.truth().codes()));
            bj.put("selectedRows", r.output().rowCount());
            bj.put("crossCheck", r.crossCheck());
            bj.put("rows", encodeRows(r.output()));
            batchJson.add(bj);
            totalSelected += r.output().rowCount();
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("ok", true);
        out.put("outputSchema", encodeSchema(outputNames, outputTypes));
        out.put("totalSelectedRows", totalSelected);
        out.put("batches", batchJson);
        return out;
    }

    private static List<String> encodeTruth(byte[] codes) {
        List<String> out = new ArrayList<>(codes.length);
        for (byte code : codes) {
            out.add(com.tvl.core.Ternary.ofCode(code).name());
        }
        return out;
    }

    private static List<Object> encodeSchema(List<String> names, List<DataType> types) {
        List<Object> out = new ArrayList<>(names.size());
        for (int i = 0; i < names.size(); i++) {
            Map<String, Object> c = new LinkedHashMap<>();
            c.put("name", names.get(i));
            c.put("type", types.get(i).sqlName());
            out.add(c);
        }
        return out;
    }

    private static List<Object> encodeRows(Batch batch) {
        List<Object> rows = new ArrayList<>(batch.rowCount());
        List<String> names = batch.schema().names();
        for (int r = 0; r < batch.rowCount(); r++) {
            Map<String, Object> row = new LinkedHashMap<>();
            for (int c = 0; c < names.size(); c++) {
                row.put(names.get(c), batch.column(c).valueAt(r));
            }
            rows.add(row);
        }
        return rows;
    }

    public static Map<String, Object> error(String code, String message) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("ok", false);
        m.put("errorCode", code);
        m.put("error", message);
        return m;
    }

    private static String requireString(Map<?, ?> map, String key) {
        Object v = map.get(key);
        if (!(v instanceof String s) || s.isBlank()) {
            throw new JsonException("字段 '" + key + "' 必须是非空字符串");
        }
        return s;
    }

    private static List<?> asArray(Object v, String key) {
        if (v == null) {
            return List.of();
        }
        if (!(v instanceof List<?> list)) {
            throw new JsonException("字段 '" + key + "' 必须是数组");
        }
        return list;
    }
}
