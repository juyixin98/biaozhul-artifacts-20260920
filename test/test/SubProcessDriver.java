package test;

import txsnapshot.Fault;
import txsnapshot.StreamEngine;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Comparator;

/**
 * 子进程驱动：在独立 JVM 中打开引擎，预填若干输入后武装 HALT 故障，
 * 再摄入触发故障的那一条；Runtime.halt(0) 会直接杀死本进程。
 *
 * 参数：<dataDir> <prefill> <point> <targetOffset>
 * 输入 i 的 value = 10 + i。
 */
public final class SubProcessDriver {

    public static void main(String[] args) throws Exception {
        Path data = Path.of(args[0]);
        int prefill = Integer.parseInt(args[1]);
        Fault.Point point = Fault.Point.valueOf(args[2]);
        long target = Long.parseLong(args[3]);

        Files.createDirectories(data);
        Fault fault = new Fault();
        StreamEngine engine = StreamEngine.open(data, fault);

        for (int i = 0; i < prefill; i++) {
            long off = engine.ingest(10L + i);
            System.err.println("[driver] ingested offset=" + off);
        }
        System.err.flush();

        fault.arm(point, Fault.Mode.HALT, target);
        try {
            long off = engine.ingest(10L + target);
            System.err.println("[driver] unexpected success offset=" + off + " (fault did not fire)");
            System.err.flush();
            Runtime.getRuntime().halt(3);
        } catch (Exception e) {
            System.err.println("[driver] unexpected exception: " + e);
            System.err.flush();
            Runtime.getRuntime().halt(4);
        }
    }

    static void deleteRecursively(Path dir) throws Exception {
        if (!Files.exists(dir)) return;
        try (var walk = Files.walk(dir)) {
            walk.sorted(Comparator.reverseOrder()).forEach(p -> {
                try { Files.deleteIfExists(p); } catch (Exception ignored) {}
            });
        }
    }
}
