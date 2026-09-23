package colscan.server;

import colscan.store.Catalog;

import java.nio.file.Path;

/** 服务入口。参数通过系统属性 / 环境变量传入（见 README）。 */
public final class Main {

    private Main() {}

    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(System.getProperty("server.port",
                System.getenv().getOrDefault("PORT", "8080")));
        String host = System.getProperty("server.host",
                System.getenv().getOrDefault("HOST", "0.0.0.0"));
        String dir = System.getProperty("data.dir",
                System.getenv().getOrDefault("DATA_DIR", "./data"));

        Catalog catalog = new Catalog(Path.of(dir));
        HttpApi.newServer(host, port, catalog);
        System.out.println("列式分片扫描服务已启动: http://localhost:" + port
                + "  数据目录: " + Path.of(dir).toAbsolutePath());
    }
}
