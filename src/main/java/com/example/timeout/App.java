package com.example.timeout;

import com.example.timeout.api.TimeoutHttpServer;
import com.example.timeout.service.SeedSpec;
import com.example.timeout.service.ServiceManager;
import com.example.timeout.tz.TzdbInfo;
import com.example.timeout.rule.DeadlineRule;

import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalTime;
import java.time.ZoneId;
import java.util.List;

/**
 * 启动入口。纯后端服务，无前端。
 *
 * <pre>
 * java -jar monotonic-timeout.jar [--mode virtual|system] [--port 8080]
 *      [--store data/timeouts.json] [--start-wall 2026-09-25T10:00:00Z] [--no-seed]
 * </pre>
 */
public final class App {

    private App() {
    }

    public static void main(String[] args) throws Exception {
        String mode = "virtual";
        int port = 8080;
        Path store = Path.of("data", "timeouts.json");
        Instant startWall = Instant.parse("2026-09-25T10:00:00Z");
        boolean seed = true;

        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--mode" -> mode = args[++i];
                case "--port" -> port = Integer.parseInt(args[++i]);
                case "--store" -> store = Path.of(args[++i]);
                case "--start-wall" -> startWall = Instant.parse(args[++i]);
                case "--no-seed" -> seed = false;
                case "--help", "-h" -> {
                    printHelp();
                    return;
                }
                default -> throw new IllegalArgumentException("unknown argument: " + args[i]);
            }
        }

        System.out.println("== Monotonic Timeout Conversion Service ==");
        System.out.println("tzdb version : " + TzdbInfo.version());
        System.out.println("java version : " + TzdbInfo.javaVersion());
        System.out.println("mode         : " + mode);
        System.out.println("store        : " + store.toAbsolutePath());

        ServiceManager manager = "system".equals(mode)
                ? ServiceManager.system(store)
                : ServiceManager.virtual(startWall, store);

        if (seed) {
            manager.current().seedIfEmpty(fixedSeeds(startWall));
            System.out.println("seed data    : loaded (fixed local test data, if store was empty)");
        }

        TimeoutHttpServer http = new TimeoutHttpServer(manager, port);
        http.start();
        System.out.println("listening    : http://localhost:" + http.port());
        System.out.println("virtual wall : " + manager.current().wallNow()
                + " (mono=" + manager.current().monoNow() + "ns)");
        System.out.println("tip          : POST /clock/tick, /clock/set-wall, /clock/advance-wall; "
                + "POST /admin/restart to recompute after restart");

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("shutting down, persisting wall-clock deadlines...");
            manager.current().persist();
            http.stop();
        }));
    }

    /**
     * 本地固定测试数据：覆盖时长规则、每日本地时刻（跨时区/DST）、版本 TTL 与绝对截止。
     * 与启动墙钟相关的种子以 {@code startWall} 为锚点，保证演示可重复。
     */
    private static List<SeedSpec> fixedSeeds(Instant startWall) {
        return List.of(
                new SeedSpec("seed-duration-10m", "fixed: ten minute duration",
                        DeadlineRule.duration(Duration.ofMinutes(10))),
                new SeedSpec("seed-daily-shanghai", "fixed: daily 03:30 Asia/Shanghai",
                        DeadlineRule.dailyLocal(LocalTime.of(3, 30), ZoneId.of("Asia/Shanghai"))),
                new SeedSpec("seed-version-ttl", "fixed: version issued at start, ttl 15m",
                        DeadlineRule.versionTtl(startWall, Duration.ofMinutes(15))),
                new SeedSpec("seed-absolute-1h", "fixed: absolute one hour after start",
                        DeadlineRule.absolute(startWall.plus(Duration.ofHours(1)))),
                new SeedSpec("seed-already-expired", "fixed: deadline one hour in the past",
                        DeadlineRule.absolute(startWall.minus(Duration.ofHours(1))))
        );
    }

    private static void printHelp() {
        System.out.println("""
                Usage: java -jar monotonic-timeout.jar [options]
                  --mode virtual|system   clock mode (default: virtual)
                  --port N                HTTP port (default: 8080)
                  --store PATH            JSON store file (default: data/timeouts.json)
                  --start-wall INSTANT    virtual starting wall clock, ISO-8601 UTC
                  --no-seed               do not load fixed test data
                  -h, --help              show this help
                """);
    }
}
