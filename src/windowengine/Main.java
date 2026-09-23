package windowengine;

import windowengine.engine.WindowEngine;

import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * JSON 请求入口（纯后端，无任何 HTTP / 前端依赖）。
 *
 * 用法：
 *   java windowengine.Main [请求文件.json ...]
 *
 * - 提供文件路径：依次执行每个请求文件（相对路径相对于进程工作目录解析）；
 * - 不提供参数：从标准输入读取一个 JSON 请求；
 * - 每个请求独立输出一行（多个文件时分隔输出），stdout 为结果 JSON，
 *   错误以 {"ok":false,"error":{...}} 输出并以非零码退出。
 *
 * 退出码：0 成功；2 请求/执行错误（错误码在 JSON 中）；1 用法/IO 级错误。
 */
public final class Main {

    public static void main(String[] args) {
        if (args.length == 0) {
            String text;
            try {
                text = new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
            } catch (Exception e) {
                System.err.println("读取标准输入失败: " + e.getMessage());
                System.exit(1);
                return;
            }
            boolean ok = runOne(text, "<stdin>");
            System.exit(ok ? 0 : 2);
        }

        boolean allOk = true;
        for (int i = 0; i < args.length; i++) {
            if (args.length > 1) {
                System.out.println("# ===== " + args[i] + " =====");
            }
            String text;
            try {
                text = java.nio.file.Files.readString(java.nio.file.Path.of(args[i]),
                        StandardCharsets.UTF_8);
            } catch (Exception e) {
                System.out.println(Json.writePretty(errorBody("IO_ERROR",
                        "无法读取请求文件: " + e.getMessage())));
                allOk = false;
                continue;
            }
            allOk &= runOne(text, args[i]);
        }
        System.exit(allOk ? 0 : 2);
    }

    /** 执行单个请求文本，打印结果；成功返回 true。 */
    static boolean runOne(String text, String source) {
        try {
            Object node = Json.parse(text);
            QueryRequest request = new RequestParser().parse(node);
            Relation result = new WindowEngine().execute(request);

            Map<String, Object> response = new LinkedHashMap<>();
            response.put("ok", true);
            response.put("source", source);
            response.put("rowCount", result.rowCount());
            response.put("result", JsonCodec.relationToJson(result));

            if (request.exportDir() != null) {
                Exporter.ExportedFiles files = new Exporter()
                        .export(request, result, request.exportDir());
                Map<String, Object> exp = new LinkedHashMap<>();
                exp.put("dir", files.dir().toString());
                exp.put("data", files.dataFile().toString());
                exp.put("plan", files.planFile().toString());
                exp.put("request", files.requestFile().toString());
                response.put("exported", exp);
            }
            System.out.println(Json.writePretty(response));
            return true;
        } catch (EngineException e) {
            System.out.println(Json.writePretty(errorBody(e.code(), e.getMessage())));
            return false;
        } catch (Exception e) {
            System.out.println(Json.writePretty(errorBody("INTERNAL_ERROR",
                    e.getClass().getSimpleName() + ": " + e.getMessage())));
            return false;
        }
    }

    private static Map<String, Object> errorBody(String code, String message) {
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("code", code);
        err.put("message", message);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("ok", false);
        body.put("error", err);
        return body;
    }
}
