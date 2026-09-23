package txsnapshot;

import java.nio.file.Path;

/**
 * 启动入口。
 *
 * 服务模式：
 *   java -cp build/classes txsnapshot.Main --port 8080 --data ./data [--debug]
 *
 * 只读校验模式（不修改任何文件）：
 *   java -cp build/classes txsnapshot.Main --verify --data ./data
 * 校验：每条 commit 标记都有对应暂存段且内容可解析；可见输出偏移恰好为 0..k-1
 * （无重复、无空洞）；状态偏移与可见条数一致。
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        int port = 8080;
        Path data = Path.of("data");
        boolean debug = false;
        boolean verify = false;

        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--port": port = Integer.parseInt(args[++i]); break;
                case "--data": data = Path.of(args[++i]); break;
                case "--debug": debug = true; break;
                case "--verify": verify = true; break;
                case "--help", "-h": printHelpAndExit(0); break;
                default:
                    System.err.println("unknown argument: " + args[i]);
                    printHelpAndExit(2);
            }
        }

        if (verify) {
            int rc = Verifier.run(data);
            System.exit(rc);
        }

        ApiServer api = ApiServer.start(data, port, debug);
        System.out.println("tx-snapshot server listening on http://127.0.0.1:" + api.getPort()
                + " data=" + data.toAbsolutePath() + (debug ? " [debug fault injection ON]" : ""));
        Runtime.getRuntime().addShutdownHook(new Thread(() -> api.stop()));
        // 常驻
        Thread.currentThread().join();
    }

    private static void printHelpAndExit(int rc) {
        System.err.println("""
            Usage:
              server: java txsnapshot.Main [--port 8080] [--data ./data] [--debug]
              verify: java txsnapshot.Main --verify --data ./data
            Endpoints:
              GET  /health
              POST /ingest        body: {"value": <integer>}
              GET  /state
              GET  /outputs
              POST /debug/fault   (only with --debug) body:
                                   {"point":"AFTER_STATE_PERSISTED","mode":"HALT","offset":3}
            """);
        System.exit(rc);
    }
}
