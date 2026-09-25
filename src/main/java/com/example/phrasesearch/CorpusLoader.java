package com.example.phrasesearch;

import com.example.phrasesearch.json.Json;
import com.example.phrasesearch.json.JsonException;
import com.example.phrasesearch.model.Document;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 外部语料加载器。文件格式：
 * <pre>
 * {
 *   "documents": [
 *     {"id": "d1", "fields": {"title": "...", "body": "..."}}
 *   ]
 * }
 * </pre>
 * fields 必须是 JSON 对象（字段顺序按对象内出现顺序保留）。
 */
public final class CorpusLoader {

    private CorpusLoader() {
    }

    @SuppressWarnings("unchecked")
    public static List<Document> load(Path path) throws IOException {
        String content = Files.readString(path);
        return parse(content);
    }

    public static List<Document> load(String path) throws IOException {
        return load(Path.of(path));
    }

    @SuppressWarnings("unchecked")
    static List<Document> parse(String content) {
        Object root;
        try {
            root = Json.parse(content);
        } catch (JsonException e) {
            throw new IllegalArgumentException("invalid corpus json: " + e.getMessage(), e);
        }
        if (!(root instanceof Map<?, ?> rootMap)) {
            throw new IllegalArgumentException("corpus root must be an object");
        }
        Object docsObj = rootMap.get("documents");
        if (!(docsObj instanceof List<?> rawDocs)) {
            throw new IllegalArgumentException("corpus must contain a 'documents' array");
        }
        List<Document> docs = new ArrayList<>();
        for (Object item : rawDocs) {
            if (!(item instanceof Map<?, ?> dm)) {
                throw new IllegalArgumentException("each document must be an object");
            }
            Object id = dm.get("id");
            Object fields = dm.get("fields");
            if (id == null || !(fields instanceof Map<?, ?> fmap)) {
                throw new IllegalArgumentException("document requires string 'id' and object 'fields'");
            }
            LinkedHashMap<String, String> fieldValues = new LinkedHashMap<>();
            for (Map.Entry<?, ?> e : fmap.entrySet()) {
                if (e.getValue() == null) {
                    continue;
                }
                fieldValues.put(String.valueOf(e.getKey()), String.valueOf(e.getValue()));
            }
            docs.add(new Document(String.valueOf(id), fieldValues));
        }
        return docs;
    }
}
