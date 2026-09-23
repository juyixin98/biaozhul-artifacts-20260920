package ppd;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * 命令行入口（JSON 请求入口）。
 *
 * <pre>
 *   java -cp out ppd.Main samples/01_scan_filter.json
 *   cat req.json | java -cp out ppd.Main
 *   java -cp out ppd.Main req.json --export result_export.json
 * </pre>
 */
public final class Main {

    private Main() {}

    public static void main(String[] args) throws Exception {
        String exportPath = null;
        String inputPath = null;
        for (int i = 0; i < args.length; i++) {
            if ("--export".equals(args[i])) {
                exportPath = args[++i];
            } else {
                inputPath = args[i];
            }
        }

        String request;
        if (inputPath != null) {
            request = Files.readString(Path.of(inputPath), StandardCharsets.UTF_8);
        } else {
            request = new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
        }

        QueryEngine engine = new QueryEngine();
        QueryEngine.Response resp = engine.runJson(request);
        String out = Json.renderPretty(resp.toJson());
        System.out.println(out);

        if (exportPath != null && resp.ok()) {
            java.util.Map<?, ?> r = Json.obj(resp.payload());
            Object dataExport = r.get("dataExport");
            Files.writeString(Path.of(exportPath),
                    Json.renderPretty(dataExport), StandardCharsets.UTF_8);
            System.err.println("[export] 数据与执行计划已导出到 " + exportPath);
        }

        if (!resp.ok()) System.exit(1);
    }
}
