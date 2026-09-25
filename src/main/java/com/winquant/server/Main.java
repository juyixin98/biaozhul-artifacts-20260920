package com.winquant.server;

import com.winquant.Clock;
import com.winquant.ManualClock;
import com.winquant.SystemClock;

/**
 * 服务入口。
 *
 * <pre>
 * 用法: java com.winquant.server.Main [--port 8080] [--window 60000] [--clock system|manual] [--start 0]
 * </pre>
 *
 * --clock manual 时可通过 POST /advance {"now":t} 推进时间，便于演示与联调。
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        int port = 8080;
        long window = 60_000;
        String clockKind = "system";
        long start = 0;
        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--port":
                    port = Integer.parseInt(args[++i]);
                    break;
                case "--window":
                    window = Long.parseLong(args[++i]);
                    break;
                case "--clock":
                    clockKind = args[++i];
                    break;
                case "--start":
                    start = Long.parseLong(args[++i]);
                    break;
                default:
                    System.err.println("unknown argument: " + args[i]);
                    System.exit(2);
            }
        }
        Clock clock;
        switch (clockKind) {
            case "system":
                clock = new SystemClock();
                break;
            case "manual":
                clock = new ManualClock(start);
                break;
            default:
                System.err.println("unknown clock: " + clockKind + " (expected system|manual)");
                System.exit(2);
                return;
        }
        QuantileServer server = new QuantileServer(port, window, clock);
        server.start();
        System.out.printf("quantile server listening on port %d, window=%d, clock=%s%n",
                server.port(), window, clockKind);
    }
}
