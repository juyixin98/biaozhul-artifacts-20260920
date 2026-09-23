package dev.dedup.hll;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.Map;

/**
 * JSON 请求入口（单机、批处理一次一个请求文件）。
 *
 * 用法：
 *   java dev.dedup.hll.Main [选项] [请求文件.json]
 *     不给文件则从标准输入读取请求。
 *   选项：
 *     --plan-out <文件>   额外把执行计划单独写到该文件（响应中也始终包含 executionPlan）
 *     --compact           输出不缩进的 JSON
 *
 * 退出码：
 *   0 请求被引擎成功处理（ok=true）
 *   1 引擎返回业务错误（ok=false，如配置不兼容、坏草图格式、草图不存在）
 *   2 请求本身无法读取/解析（IO 错误或 JSON 语法错误）
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws IOException {
        String requestFile = null;
        String planOut = null;
        boolean compact = false;
        for (int i = 0; i < args.length; i++) {
            String a = args[i];
            switch (a) {
                case "--plan-out":
                    if (i + 1 >= args.length) {
                        System.err.println("--plan-out 缺少文件参数");
                        System.exit(2);
                    }
                    planOut = args[++i];
                    break;
                case "--compact":
                    compact = true;
                    break;
                case "-h":
                case "--help":
                    System.out.println("用法: java dev.dedup.hll.Main [--plan-out f] [--compact] [request.json | -]");
                    System.out.println("      request.json 省略或为 - 时从标准输入读取");
                    return;
                default:
                    if (a.startsWith("--")) {
                        System.err.println("未知选项: " + a);
                        System.exit(2);
                    }
                    requestFile = a;
            }
        }

        String text;
        try {
            if (requestFile == null || "-".equals(requestFile)) {
                text = new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
            } else {
                text = new String(Files.readAllBytes(Paths.get(requestFile)), StandardCharsets.UTF_8);
            }
        } catch (IOException ioe) {
            System.err.println("读取请求失败: " + ioe.getMessage());
            System.exit(2);
            return;
        }

        Object parsed;
        try {
            parsed = Json.parse(text);
        } catch (Json.JsonException je) {
            System.out.println(Json.write(fatal("MALFORMED_JSON", je.getMessage()), !compact));
            System.exit(2);
            return;
        }
        if (!(parsed instanceof Map)) {
            System.out.println(Json.write(fatal("MALFORMED_JSON", "请求顶层必须是 JSON 对象"), !compact));
            System.exit(2);
            return;
        }

        @SuppressWarnings("unchecked")
        Map<String, Object> request = (Map<String, Object>) parsed;
        Map<String, Object> response = new QueryEngine().execute(request);

        if (planOut != null) {
            Object plan = response.get("executionPlan");
            if (plan != null) {
                Files.write(Paths.get(planOut),
                        Json.write(plan, true).getBytes(StandardCharsets.UTF_8));
            }
        }

        System.out.println(Json.write(response, !compact));
        System.exit(Boolean.TRUE.equals(response.get("ok")) ? 0 : 1);
    }

    private static Map<String, Object> fatal(String code, String message) {
        java.util.Map<String, Object> resp = new java.util.LinkedHashMap<>();
        resp.put("ok", false);
        java.util.Map<String, Object> err = new java.util.LinkedHashMap<>();
        err.put("code", code);
        err.put("message", message);
        err.put("failedOpIndex", -1);
        resp.put("error", err);
        return resp;
    }
}
