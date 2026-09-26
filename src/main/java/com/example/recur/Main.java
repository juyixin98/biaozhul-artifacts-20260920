package com.example.recur;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * 纯后端 CLI：读取本地 JSON 请求文件并打印 JSON 响应。无 HTTP 服务、无前端。
 *
 * <pre>
 *   java -cp ... com.example.recur.Main samples/01-daily.json
 *   cat request.json | java -cp ... com.example.recur.Main -
 * </pre>
 */
public final class Main {

  private Main() {
  }

  public static void main(String[] args) throws IOException {
    String json;
    if (args.length == 0 || "-".equals(args[0])) {
      json = new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
    } else {
      json = Files.readString(Path.of(args[0]), StandardCharsets.UTF_8);
    }
    String output = new RecurrenceService().handleJson(json);
    System.out.println(output);
  }
}
