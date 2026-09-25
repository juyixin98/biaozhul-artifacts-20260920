package booleansearch;

import booleansearch.corpus.SyntheticCorpus;
import booleansearch.index.InvertedIndex;
import booleansearch.server.SearchHttpServer;

/**
 * 服务入口：装载合成语料并启动本地 JSON HTTP 服务。
 *
 * <p>参数（均可选）：{@code --port 8080}（默认 8080）、{@code --host 127.0.0.1}（默认本机回环）。
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        int port = 8080;
        String host = "127.0.0.1";
        for (int i = 0; i < args.length; i++) {
            if ("--port".equals(args[i]) && i + 1 < args.length) {
                port = Integer.parseInt(args[++i]);
            } else if ("--host".equals(args[i]) && i + 1 < args.length) {
                host = args[++i];
            } else {
                System.err.println("未知参数: " + args[i] + "（支持 --port N --host H）");
                System.exit(2);
            }
        }

        InvertedIndex index = new InvertedIndex();
        SyntheticCorpus.loadInto(index);

        SearchHttpServer server = new SearchHttpServer(index, host, port);
        server.start();
        Runtime.getRuntime().addShutdownHook(new Thread(server::stop));

        System.out.println("布尔检索服务已启动");
        System.out.println("  语料文档数: " + index.docCount());
        System.out.println("  存活词项数: " + index.termCount());
        System.out.println("  监听地址:   http://" + host + ":" + server.getPort());
        System.out.println("  示例: curl -s http://" + host + ":" + server.getPort() + "/stats");
    }
}
