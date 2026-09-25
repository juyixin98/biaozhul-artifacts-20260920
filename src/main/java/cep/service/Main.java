package cep.service;

/**
 * 服务入口。
 *
 * <pre>
 *   java -cp build/classes cep.service.Main [port]
 *   PORT=8080 java -cp build/classes cep.service.Main
 * </pre>
 *
 * 默认端口 8080；端口 0 表示由系统分配（测试用）。
 */
public final class Main {

    private Main() {}

    public static void main(String[] args) {
        int port = 8080;
        if (args.length >= 1) {
            port = Integer.parseInt(args[0]);
        } else if (System.getenv("PORT") != null) {
            port = Integer.parseInt(System.getenv("PORT"));
        }
        HttpApiServer server = new HttpApiServer(port);
        server.start();
        System.out.println("流式模式匹配服务已启动: http://localhost:" + server.getPort());
        System.out.println("  POST /evaluate             一次性确定性评估");
        System.out.println("  POST /api/sessions         创建会话");
        System.out.println("  GET  /healthz              健康检查");

        Runtime.getRuntime().addShutdownHook(new Thread(server::stop));
    }
}
