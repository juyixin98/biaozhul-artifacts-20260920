package com.bm25stable;

/**
 * 一条文档记录。id 为字符串类型，分页同分时按 id 字典序升序作为决胜键。
 */
public record Document(String id, String text) {
    public Document {
        if (id == null || id.isBlank()) {
            throw new IllegalArgumentException("document id must not be blank");
        }
        if (text == null) {
            text = "";
        }
    }
}
