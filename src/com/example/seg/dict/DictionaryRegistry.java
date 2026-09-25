package com.example.seg.dict;

import java.io.IOException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

/**
 * 词典版本注册表：启动时一次性加载目录下全部 *.dict，之后版本切换只是换引用，
 * 不重新解析文件，天然支持"字典版本切换"验收点。
 */
public final class DictionaryRegistry {

    private final Map<String, Dictionary> dictionaries = new LinkedHashMap<>();

    public static DictionaryRegistry loadDirectory(Path dir) throws IOException {
        DictionaryRegistry registry = new DictionaryRegistry();
        try (DirectoryStream<Path> stream = Files.newDirectoryStream(dir, "*.dict")) {
            for (Path file : stream) {
                Dictionary dict = Dictionary.load(file);
                registry.dictionaries.put(dict.version(), dict);
            }
        }
        if (registry.dictionaries.isEmpty()) {
            throw new IOException("目录 " + dir + " 下没有找到 *.dict 词典文件");
        }
        return registry;
    }

    /** 供测试用：直接注册构造好的词典。 */
    public void register(Dictionary dict) {
        dictionaries.put(dict.version(), dict);
    }

    /** 取词典版本；不存在返回 null（由服务层映射成 404）。 */
    public Dictionary get(String version) {
        return dictionaries.get(version);
    }

    public Set<String> versions() {
        return dictionaries.keySet();
    }
}
