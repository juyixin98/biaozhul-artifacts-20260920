package approx;

import java.io.IOException;

import approx.server.HttpEventServer;
import approx.time.Clock;
import approx.time.ManualEnvironment;
import approx.time.SystemRuntime;
import approx.time.TaskScheduler;

/**
 * Server entry point.
 *
 * <pre>
 * java approx.Main [--port 8080] [--manual-time]
 * </pre>
 *
 * With {@code --manual-time} the clock is virtual and only moves when
 * {@code POST /v1/admin/advance-time {"millis":N}} is called &mdash; useful for
 * reproducible demos and tests without sleeping.
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws IOException, InterruptedException {
        int port = 8080;
        boolean manualTime = false;
        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--port":
                    port = Integer.parseInt(args[++i]);
                    break;
                case "--manual-time":
                    manualTime = true;
                    break;
                case "--help":
                case "-h":
                    System.out.println("Usage: java approx.Main [--port 8080] [--manual-time]");
                    return;
                default:
                    System.err.println("unknown argument: " + args[i]);
                    System.exit(2);
            }
        }

        Clock clock;
        TaskScheduler scheduler;
        ManualEnvironment manual = null;
        if (manualTime) {
            manual = new ManualEnvironment(0L);
            clock = manual;
            scheduler = manual;
        } else {
            SystemRuntime runtime = new SystemRuntime("approx-scheduler");
            clock = runtime;
            scheduler = runtime;
        }

        HttpEventServer server = HttpEventServer.start(port, clock, scheduler, manualTime);
        if (manual != null) {
            ManualEnvironment env = manual;
            server.setTimeAdvancer(env::advance);
        }
        System.out.println("approx-frequent-items listening on http://localhost:" + server.port()
                + (manualTime ? " (manual time, now=0)" : " (system time)"));

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            server.close();
            scheduler.close();
        }));

        // Block forever; shutdown hook (Ctrl-C) stops the server.
        Thread.currentThread().join();
    }
}
