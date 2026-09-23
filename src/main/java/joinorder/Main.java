package joinorder;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 命令行入口。
 *
 *   java joinorder.Main request.json [response.json] [--plan-out p.json]
 *                                   [--data-out d.json] [--dp-out dp.json]
 *
 * 不带参数时从 stdin 读取请求；不指定输出文件时把响应打印到 stdout。
 * 退出码：0 成功；2 请求/执行错误（错误 JSON 输出到 stdout）；1 其它 IO 错误。
 */
public final class Main {

    public static void main(String[] args) throws IOException {
        String requestFile = null;
        String responseFile = null;
        String planOut = null;
        String dataOut = null;
        String dpOut = null;

        for (int i = 0; i < args.length; i++) {
            String a = args[i];
            switch (a) {
                case "--plan-out": planOut = args[++i]; break;
                case "--data-out": dataOut = args[++i]; break;
                case "--dp-out": dpOut = args[++i]; break;
                default:
                    if (requestFile == null) requestFile = a;
                    else if (responseFile == null) responseFile = a;
                    else throw new EngineException("无法识别的参数: " + a);
            }
        }

        String reqText;
        if (requestFile != null) {
            reqText = Files.readString(Path.of(requestFile), StandardCharsets.UTF_8);
        } else {
            reqText = new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
        }

        Object parsed;
        try {
            parsed = Json.parse(reqText);
        } catch (EngineException e) {
            emit(responseFile, error("JSON_PARSE", e.getMessage()), true);
            System.exit(2);
            return;
        }
        if (!(parsed instanceof Map)) {
            emit(responseFile, error("BAD_REQUEST", "请求必须是 JSON 对象"), true);
            System.exit(2);
            return;
        }

        Map<String, Object> response;
        try {
            response = new Engine().run(Json.asObject(parsed, "request"));
        } catch (EngineException e) {
            emit(responseFile, error("ENGINE_ERROR", e.getMessage()), true);
            System.exit(2);
            return;
        }

        emit(responseFile, response, false);

        // 可选导出：执行计划
        if (planOut != null) {
            Map<String, Object> doc = new LinkedHashMap<>();
            doc.put("query", response.get("query"));
            doc.put("strategy", response.get("strategy"));
            doc.put("plan", response.get("plan"));
            doc.put("planText", response.get("planText"));
            Files.writeString(Path.of(planOut), Json.write(doc), StandardCharsets.UTF_8);
        }
        // 可选导出：规范化后的数据集（行用 表名.列名 键）
        if (dataOut != null) {
            Map<String, Object> req = Json.asObject(parsed, "request");
            Files.writeString(Path.of(dataOut), Json.write(req.get("tables")), StandardCharsets.UTF_8);
        }
        // 可选导出：DP 表
        if (dpOut != null) {
            Map<String, Object> doc = new LinkedHashMap<>();
            doc.put("query", response.get("query"));
            doc.put("dpTable", response.get("dpTable"));
            Files.writeString(Path.of(dpOut), Json.write(doc), StandardCharsets.UTF_8);
        }
    }

    private static void emit(String responseFile, Map<String, Object> payload, boolean prettyStderr)
            throws IOException {
        String text = Json.write(payload);
        if (responseFile != null) {
            Files.writeString(Path.of(responseFile), text, StandardCharsets.UTF_8);
            if (payload.containsKey("error")) {
                Map<?, ?> e = (Map<?, ?>) payload.get("error");
                System.err.println(e.get("code") + ": " + e.get("message"));
            } else {
                System.out.println("响应已写入 " + responseFile);
            }
        } else {
            System.out.print(text);
        }
    }

    private static Map<String, Object> error(String code, String message) {
        Map<String, Object> e = new LinkedHashMap<>();
        e.put("code", code);
        e.put("message", message);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("ok", false);
        body.put("error", e);
        return body;
    }
}
