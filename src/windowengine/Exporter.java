package windowengine;

import windowengine.plan.QueryPlan;

import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 导出器：把数据、执行计划和规范化请求写入目录。
 *   data.json    —— 输出关系（也可用作回读数据源）
 *   plan.json    —— 规范化执行计划（默认帧/NULL 顺序全部显式展开）
 *   request.json —— 规范化后的完整请求（输入数据 + 计划），可再次提交执行
 */
public final class Exporter {

    /**
     * 导出器：把数据、执行计划和规范化请求写入目录。
     *   data.json    —— 输出关系（含窗口结果列；可用作新查询的输入数据源）
     *   plan.json    —— 规范化执行计划（默认帧/NULL 顺序全部显式展开）
     *   request.json —— 规范化后的完整请求，data 为【原始输入数据】，
     *                   因此该文件可以原样再次提交执行并复现同样结果
     */
    public record ExportedFiles(Path dir, Path dataFile, Path planFile, Path requestFile) {
    }

    public ExportedFiles export(QueryRequest request, Relation result, String directory) {
        Path dir = Path.of(directory);

        QueryPlan plan = request.plan();
        Path dataFile = dir.resolve("data.json");
        Path planFile = dir.resolve("plan.json");
        Path requestFile = dir.resolve("request.json");

        Json.writeFile(dataFile, JsonCodec.relationToJson(result), true);
        Json.writeFile(planFile, JsonCodec.planToJson(plan), true);

        Map<String, Object> normalized = new LinkedHashMap<>();
        normalized.put("data", JsonCodec.relationToJson(request.data()));
        normalized.put("plan", JsonCodec.planToJson(plan));
        Json.writeFile(requestFile, normalized, true);

        return new ExportedFiles(dir, dataFile, planFile, requestFile);
    }
}
