package com.bm25pager.model;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 不可变文档。docId 唯一，content 为原始正文（允许为空串），
 * metadata 是附加字段（如 title），不参与索引但会随命中文档返回。
 */
public final class Document {

    private final String docId;
    private final String content;
    private final Map<String, String> metadata;

    public Document(String docId, String content, Map<String, String> metadata) {
        if (docId == null || docId.isEmpty()) {
            throw new IllegalArgumentException("docId must not be null or empty");
        }
        this.docId = docId;
        this.content = content == null ? "" : content;
        this.metadata = metadata == null
                ? Collections.emptyMap()
                : Collections.unmodifiableMap(new LinkedHashMap<>(metadata));
    }

    public String docId() {
        return docId;
    }

    public String content() {
        return content;
    }

    public Map<String, String> metadata() {
        return metadata;
    }
}
