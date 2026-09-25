package com.example.phrasesearch.model;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 一篇被检索的文档：由若干有序命名字段组成。
 * 字段顺序很重要——跨字段短语按该声明顺序把各字段 token 流拼接成全局位置流。
 */
public final class Document {

    private final String id;
    private final LinkedHashMap<String, String> fields;

    public Document(String id, LinkedHashMap<String, String> fields) {
        this.id = id;
        this.fields = new LinkedHashMap<>(fields);
    }

    public Document(String id, Map<String, String> fields) {
        this(id, new LinkedHashMap<>(fields));
    }

    public String id() {
        return id;
    }

    /** 按声明顺序返回字段名。 */
    public Set<String> fieldNames() {
        return fields.keySet();
    }

    public List<String> orderedFieldNames() {
        return List.copyOf(fields.keySet());
    }

    public String field(String name) {
        return fields.get(name);
    }

    public Map<String, String> fields() {
        return fields;
    }
}
