package com.example.seg.dict;

import com.example.seg.model.Costs;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.Reader;
import java.io.StringReader;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 一个"词典版本"：词 -> 声明代价（BigDecimal，原始精度），外加未知单字代价。
 * 内部建一棵 Trie 供分词时枚举候选边。
 *
 * 文件格式（UTF-8 文本，# 开头为注释）：
 * <pre>
 * # @unknown-cost 5      未登录单字的统一代价（必填指令）
 * # @description 说明    可选
 * 研究 1.0                词 + 空白 + 代价（最多 6 位小数）
 * 研究生 2.5
 * </pre>
 */
public final class Dictionary {

    private final String version;
    private final String description;
    private final long unknownCostScaled;
    private final Trie trie;
    private final Map<String, BigDecimal> declaredCosts;

    private Dictionary(String version, String description, long unknownCostScaled,
                       Trie trie, Map<String, BigDecimal> declaredCosts) {
        this.version = version;
        this.description = description;
        this.unknownCostScaled = unknownCostScaled;
        this.trie = trie;
        this.declaredCosts = declaredCosts;
    }

    public String version() {
        return version;
    }

    public String description() {
        return description;
    }

    public long unknownCostScaled() {
        return unknownCostScaled;
    }

    public Trie trie() {
        return trie;
    }

    public int size() {
        return declaredCosts.size();
    }

    public int maxWordLength() {
        return trie.maxWordLength();
    }

    /** 查词的声明代价（原始精度），非词返回 null。 */
    public BigDecimal declaredCost(String word) {
        return declaredCosts.get(word);
    }

    public Map<String, BigDecimal> entries() {
        return Collections.unmodifiableMap(declaredCosts);
    }

    // ------------------------------------------------------------------
    // 构造 / 加载
    // ------------------------------------------------------------------

    /** 供测试用的编程式构造器。 */
    public static Builder builder(String version, long unknownCostScaled) {
        return new Builder(version, unknownCostScaled);
    }

    public static final class Builder {
        private final String version;
        private final long unknownCostScaled;
        private String description = "";
        private final Map<String, BigDecimal> entries = new LinkedHashMap<>();

        private Builder(String version, long unknownCostScaled) {
            this.version = version;
            this.unknownCostScaled = unknownCostScaled;
        }

        public Builder description(String description) {
            this.description = description;
            return this;
        }

        /** 以字符串形式添加词代价，例如 add("研究", "1.0")。 */
        public Builder add(String word, String costText) {
            long scaled = Costs.parseScaled(costText);
            entries.put(word, Costs.toBigDecimal(scaled));
            return this;
        }

        public Dictionary build() {
            Trie trie = new Trie();
            for (Map.Entry<String, BigDecimal> e : entries.entrySet()) {
                trie.put(e.getKey(), e.getValue().movePointRight(6).longValueExact());
            }
            return new Dictionary(version, description, unknownCostScaled, trie,
                    new LinkedHashMap<>(entries));
        }
    }

    /** 从文件加载词典，version 取文件名去掉 .dict 后缀。 */
    public static Dictionary load(Path file) throws IOException {
        String fileName = file.getFileName().toString();
        String version = fileName.endsWith(".dict")
                ? fileName.substring(0, fileName.length() - ".dict".length())
                : fileName;
        String content = Files.readString(file, StandardCharsets.UTF_8);
        return parse(version, new StringReader(content));
    }

    /** 解析词典文本。 */
    public static Dictionary parse(String version, Reader reader) throws IOException {
        Long unknown = null;
        String description = "";
        List<String[]> rawEntries = new ArrayList<>();

        try (BufferedReader br = new BufferedReader(reader)) {
            int lineNo = 0;
            String line;
            while ((line = br.readLine()) != null) {
                lineNo++;
                String trimmed = line.trim();
                if (trimmed.isEmpty()) {
                    continue;
                }
                if (trimmed.startsWith("#")) {
                    String body = trimmed.substring(1).trim();
                    Long directiveValue;
                    if ((directiveValue = directiveInt(body, "@unknown-cost")) != null) {
                        // 只有值确实是非负整数时才视为指令；
                        // "# @unknown-cost 未登录单字..." 这类说明文字按普通注释忽略。
                        unknown = directiveValue;
                    } else if (hasDirective(body, "@description")) {
                        description = body.substring("@description".length()).trim();
                    }
                    // 其余注释一律忽略
                    continue;
                }
                String[] parts = trimmed.split("\\s+", 2);
                if (parts.length != 2 || parts[0].isEmpty() || parts[1].isBlank()) {
                    throw new IOException("词典 " + version + " 第 " + lineNo
                            + " 行格式错误，应为 '词 代价': " + line);
                }
                rawEntries.add(new String[]{parts[0], parts[1].trim()});
            }
        }
        if (unknown == null) {
            throw new IOException("词典 " + version + " 缺少 # @unknown-cost <整数> 指令");
        }

        Builder b = builder(version, unknown).description(description);
        for (String[] e : rawEntries) {
            b.add(e[0], e[1]);
        }
        return b.build();
    }

    /** 判断注释正文是否以某指令开头，且指令后面是空白或行尾（避免把说明文字误当指令）。 */
    private static boolean hasDirective(String body, String directive) {
        if (!body.startsWith(directive)) {
            return false;
        }
        if (body.length() == directive.length()) {
            return true;
        }
        char next = body.charAt(directive.length());
        return Character.isWhitespace(next);
    }

    /**
     * 若 body 形如 "&lt;指令名&gt; &lt;非负整数&gt;"，返回放大整数代价；
     * 指令存在但值不是整数（例如说明文字）时返回 null，按普通注释处理；
     * 值是负整数时抛错。
     */
    private static Long directiveInt(String body, String directive) throws IOException {
        if (!hasDirective(body, directive)) {
            return null;
        }
        String value = body.substring(directive.length()).trim();
        int v;
        try {
            v = Integer.parseInt(value);
        } catch (NumberFormatException e) {
            return null; // "# @unknown-cost 未登录字代价..." 这类文档行
        }
        if (v < 0) {
            throw new IOException(directive + " 需要非负整数，实际为: " + value);
        }
        return Costs.ofInt(v);
    }
}
