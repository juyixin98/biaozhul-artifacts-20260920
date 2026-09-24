package com.example.smj;

import java.nio.file.Path;

/** Entry point: starts the HTTP server. */
public final class Main {

  private Main() {}

  public static void main(String[] args) throws Exception {
    int port = 8080;
    long defaultBudget = 8L << 20; // 8 MiB
    int maxConcurrent = 2;
    Path tempDir = Path.of("tmp");

    for (int i = 0; i < args.length; i++) {
      switch (args[i]) {
        case "--port" -> port = Integer.parseInt(args[++i]);
        case "--temp-dir" -> tempDir = Path.of(args[++i]);
        case "--default-budget" -> defaultBudget = Long.parseLong(args[++i]);
        case "--max-concurrent" -> maxConcurrent = Integer.parseInt(args[++i]);
        default -> {
          System.err.println("unknown argument: " + args[i]);
          printUsage();
          System.exit(2);
        }
      }
    }

    TaskManager tasks = new TaskManager(maxConcurrent, tempDir);
    HttpApi api = HttpApi.start(port, tasks, defaultBudget);
    Runtime.getRuntime().addShutdownHook(new Thread(() -> {
      api.stop();
      tasks.close();
    }));
    System.out.println("sort-merge-join server listening on port " + api.port());
    System.out.println("temp dir: " + tempDir.toAbsolutePath()
        + ", default memory budget: " + defaultBudget + " bytes"
        + ", workers: " + maxConcurrent);
    Thread.currentThread().join();
  }

  private static void printUsage() {
    System.err.println("""
        usage: java -jar sort-merge-join-1.0.0.jar [options]
          --port <n>             listen port (default 8080)
          --temp-dir <path>      directory for spill/temp files (default ./tmp)
          --default-budget <n>   default memory budget in bytes (default 8388608)
          --max-concurrent <n>   max concurrent join tasks (default 2)
        """);
  }
}
