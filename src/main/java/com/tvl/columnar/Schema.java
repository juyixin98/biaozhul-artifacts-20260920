package com.tvl.columnar;

import com.tvl.types.DataType;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 有序列模式：列名（大小写敏感）-> 类型。
 */
public final class Schema {

    private final List<String> names;
    private final Map<String, DataType> types;

    public Schema(List<String> names, Map<String, DataType> types) {
        if (names.size() != types.size()) {
            throw new IllegalArgumentException("列名与类型数量不一致");
        }
        for (String name : names) {
            if (!types.containsKey(name)) {
                throw new IllegalArgumentException("列 " + name + " 缺少类型定义");
            }
        }
        this.names = new ArrayList<>(names);
        this.types = new LinkedHashMap<>(types);
    }

    public static Schema of(String... nameTypePairs) {
        if (nameTypePairs.length % 2 != 0) {
            throw new IllegalArgumentException("必须是 (名, 类型) 成对出现");
        }
        List<String> names = new ArrayList<>();
        Map<String, DataType> map = new LinkedHashMap<>();
        for (int i = 0; i < nameTypePairs.length; i += 2) {
            String name = nameTypePairs[i];
            DataType type = DataType.parse(nameTypePairs[i + 1]);
            if (map.put(name, type) != null) {
                throw new IllegalArgumentException("重复列名: " + name);
            }
            names.add(name);
        }
        return new Schema(names, map);
    }

    public int size() {
        return names.size();
    }

    public List<String> names() {
        return Collections.unmodifiableList(names);
    }

    public DataType typeOf(String name) {
        DataType t = types.get(name);
        if (t == null) {
            throw new IllegalArgumentException("未知列: " + name);
        }
        return t;
    }

    public int indexOf(String name) {
        int i = names.indexOf(name);
        if (i < 0) {
            throw new IllegalArgumentException("未知列: " + name);
        }
        return i;
    }

    public boolean hasColumn(String name) {
        return types.containsKey(name);
    }
}
