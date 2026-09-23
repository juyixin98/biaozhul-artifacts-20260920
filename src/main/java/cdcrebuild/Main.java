package cdcrebuild;

import cdcrebuild.engine.CdcEngine;
import cdcrebuild.http.ApiServer;

import java.nio.file.Path;

/**
 * 启动入口。
 *
 *   java cdcrebuild.Main [port] [dataDir] [pkColumn]
 * 也可用环境变量覆盖：PORT / DATA_DIR / PK_COLUMN。
 * 默认：端口 8080，数据目录 ./data，主键列 id。
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(effective(args, 0, "PORT", "8080"));
        String dataDir = effective(args, 1, "DATA_DIR", "data");
        String pkColumn = effective(args, 2, "PK_COLUMN", "id");

        CdcEngine engine = CdcEngine.open(Path.of(dataDir), pkColumn);
        ApiServer server = ApiServer.start(port, engine);

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            try {
                server.stop();
                engine.close();
            } catch (Exception e) {
                System.err.println("[shutdown] " + e);
            }
        }));

        System.out.println("变更流状态重建服务已启动");
        System.out.println("  监听端口 : " + server.port());
        System.out.println("  数据目录 : " + Path.of(dataDir).toAbsolutePath());
        System.out.println("  主键列   : " + pkColumn);
        System.out.println("  健康检查 : GET  http://localhost:" + server.port() + "/healthz");
        System.out.println("  投递事件 : POST http://localhost:" + server.port() + "/v1/events");
    }

    private static String effective(String[] args, int index, String env, String def) {
        if (args.length > index && args[index] != null && !args[index].isBlank()) {
            return args[index];
        }
        String v = System.getenv(env);
        return (v == null || v.isBlank()) ? def : v;
    }
}
