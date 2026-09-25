package drvb;

import drvb.http.RuleHttpServer;
import drvb.service.DynamicRuleService;
import drvb.time.Clock;
import drvb.time.ExecutorScheduler;
import drvb.time.ManualClock;
import drvb.time.ManualScheduler;
import drvb.time.Scheduler;
import drvb.time.WallClock;

import java.util.concurrent.CountDownLatch;

/**
 * 服务入口。
 *
 * <pre>
 * java drvb.Main [--port=8080] [--clock=manual|wall] [--start-time=1000000]
 *                [--allowed-lateness=5000] [--retention-horizon=60000]
 *                [--auto-reclaim-ms=0]
 * </pre>
 *
 * <ul>
 *   <li>{@code --clock=manual}（默认）：时间静止，仅由 POST /admin/tick 推进，
 *       周期任务在 tick 时确定性补跑——验收乱序/晚到/回收边界时使用；</li>
 *   <li>{@code --clock=wall}：使用系统墙钟与真实调度线程；</li>
 *   <li>{@code --allowed-lateness}：水位线允许的乱序时长；</li>
 *   <li>{@code --retention-horizon}：历史版本回收的额外保留视窗
 *       （回收闸门 = 水位线 - 该值）；</li>
 *   <li>{@code --auto-reclaim-ms&gt;0}：注册周期性自动回收任务。</li>
 * </ul>
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        int port = 8080;
        String clockMode = "manual";
        long startTime = 1_000_000L;
        long allowedLateness = 5_000L;
        long retentionHorizon = 60_000L;
        long autoReclaimMs = 0L;

        for (String arg : args) {
            if (arg.startsWith("--port=")) {
                port = Integer.parseInt(after(arg));
            } else if (arg.startsWith("--clock=")) {
                clockMode = after(arg);
            } else if (arg.startsWith("--start-time=")) {
                startTime = Long.parseLong(after(arg));
            } else if (arg.startsWith("--allowed-lateness=")) {
                allowedLateness = Long.parseLong(after(arg));
            } else if (arg.startsWith("--retention-horizon=")) {
                retentionHorizon = Long.parseLong(after(arg));
            } else if (arg.startsWith("--auto-reclaim-ms=")) {
                autoReclaimMs = Long.parseLong(after(arg));
            } else if ("--help".equals(arg) || "-h".equals(arg)) {
                printUsageAndExit();
            } else {
                System.err.println("未知参数: " + arg);
                printUsageAndExit();
            }
        }

        Clock clock;
        Scheduler scheduler;
        if ("manual".equals(clockMode)) {
            clock = new ManualClock(startTime);
            scheduler = new ManualScheduler(clock);
        } else if ("wall".equals(clockMode)) {
            clock = new WallClock();
            scheduler = new ExecutorScheduler();
        } else {
            throw new IllegalArgumentException("--clock 只能是 manual 或 wall，实际: " + clockMode);
        }

        DynamicRuleService service =
                new DynamicRuleService(clock, scheduler, allowedLateness, retentionHorizon);
        if (autoReclaimMs > 0) {
            service.enableAutoReclaim(autoReclaimMs);
        }

        RuleHttpServer http = new RuleHttpServer(service);
        int actualPort = http.start(port);
        System.out.println("动态规则版本绑定服务已启动");
        System.out.println("  监听地址      : http://127.0.0.1:" + actualPort);
        System.out.println("  时钟模式      : " + clockMode
                + ("manual".equals(clockMode) ? "（起始 " + startTime + "，POST /admin/tick 推进）" : ""));
        System.out.println("  allowedLateness  = " + allowedLateness + " ms");
        System.out.println("  retentionHorizon = " + retentionHorizon + " ms");
        System.out.println("  自动回收周期     = "
                + (autoReclaimMs > 0 ? autoReclaimMs + " ms" : "关闭（可手动 POST /reclaim）"));

        CountDownLatch latch = new CountDownLatch(1);
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            http.stop();
            scheduler.close();
            latch.countDown();
        }, "drvb-shutdown"));
        latch.await();
    }

    private static String after(String kv) {
        return kv.substring(kv.indexOf('=') + 1);
    }

    private static void printUsageAndExit() {
        System.err.println("用法: java drvb.Main [--port=8080] [--clock=manual|wall]");
        System.err.println("          [--start-time=1000000] [--allowed-lateness=5000]");
        System.err.println("          [--retention-horizon=60000] [--auto-reclaim-ms=0]");
        System.exit(2);
    }
}
