package com.example.paginate.model;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 业务记录。排序键：name（可重复，字符串）、score（可重复，数值）。
 * id 全库唯一，作为稳定次序的最终决胜键。
 */
public final class Item {

    private final long id;
    private String name;
    private String category;
    private long score;

    public Item(long id, String name, String category, long score) {
        this.id = id;
        this.name = name;
        this.category = category;
        this.score = score;
    }

    public long id() {
        return id;
    }

    public String name() {
        return name;
    }

    public String category() {
        return category;
    }

    public long score() {
        return score;
    }

    public void setName(String name) {
        this.name = name;
    }

    public void setCategory(String category) {
        this.category = category;
    }

    public void setScore(long score) {
        this.score = score;
    }

    public Map<String, Object> toJson() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", id);
        m.put("name", name);
        m.put("category", category);
        m.put("score", score);
        return m;
    }
}
