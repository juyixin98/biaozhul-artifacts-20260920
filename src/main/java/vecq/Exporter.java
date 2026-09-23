package vecq;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 数据 / 计划 / 结果导出：把内存对象写成 JSON 文件（无外部依赖）。
 *
 * 导出内容：
 *   table.json    源表（含值数组与 NULL 位图）
 *   plan.json     逻辑执行计划
 *   result.json   查询响应（选择向量、列式投影、聚合、双引擎统计）
 */
public final class Exporter {

    private Exporter() {}

    public static void exportAll(QueryResult result) {
        String dir = result.plan().exportDir();
        if (dir == null || dir.isEmpty()) return;
        exportAll(result, Path.of(dir));
    }

    public static void exportAll(QueryResult result, Path dir) {
        try {
            Files.createDirectories(dir);
            write(dir.resolve("table.json"), result.plan().table().toJson());
            write(dir.resolve("plan.json"), result.plan().planToJson());
            write(dir.resolve("result.json"), result.toResponseJson());
        } catch (IOException e) {
            throw new RuntimeException("导出到目录 " + dir + " 失败: " + e.getMessage(), e);
        }
    }

    static Path write(Path file, Object json) throws IOException {
        Map<String, Object> wrap = new LinkedHashMap<>();
        Files.writeString(file, Json.writePretty(json));
        return file;
    }
}
