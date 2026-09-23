package com.example.tvl.engine;

import com.example.tvl.sql.DataType;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 批次的列模式：有序列定义，列名大小写不敏感。 */
public final class Schema {

    public record Column(String name, DataType type) {}

    private final List<Column> columns;
    private final Map<String, Integer> indexByName;

    public Schema(List<Column> columns) {
        this.columns = List.copyOf(columns);
        Map<String, Integer> map = new LinkedHashMap<>();
        for (int i = 0; i < columns.size(); i++) {
            String key = columns.get(i).name().toUpperCase();
            if (map.putIfAbsent(key, i) != null) {
                throw new SemanticException("模式中存在重复列名: " + columns.get(i).name());
            }
        }
        this.indexByName = Map.copyOf(map);
    }

    public List<Column> columns() {
        return columns;
    }

    public int size() {
        return columns.size();
    }

    /** 返回列下标；未知列抛 {@link SemanticException}。 */
    public int requireIndex(String name) {
        Integer idx = indexByName.get(name.toUpperCase());
        if (idx == null) {
            throw new SemanticException("未知列: " + name);
        }
        return idx;
    }

    public DataType requireType(String name) {
        return columns.get(requireIndex(name)).type();
    }

    public static Schema from(List<?> raw) {
        List<Column> cols = new ArrayList<>();
        for (Object o : raw) {
            if (!(o instanceof Map<?, ?> m)) {
                throw new SemanticException("schema.columns 元素必须是对象");
            }
            Object name = m.get("name");
            Object type = m.get("type");
            if (!(name instanceof String s) || s.isBlank()) {
                throw new SemanticException("schema.columns[].name 必须是非空字符串");
            }
            if (!(type instanceof String t)) {
                throw new SemanticException("列 " + s + " 缺少 type");
            }
            cols.add(new Column(s, DataType.parse(t)));
        }
        if (cols.isEmpty()) {
            throw new SemanticException("schema 至少需要一列");
        }
        return new Schema(cols);
    }
}
