package com.example.watermark.service;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;

import com.example.watermark.json.Json;

/**
 * Run a request JSON file in-process (no HTTP) and print the response JSON.
 * Usage: {@code java ... WatermarkScriptRunner request.json}
 */
public final class WatermarkScriptRunner {

    @SuppressWarnings("unchecked")
    public static void main(String[] args) throws Exception {
        if (args.length != 1) {
            System.err.println("usage: WatermarkScriptRunner <request.json>");
            System.exit(2);
        }
        String text = Files.readString(Paths.get(args[0]), StandardCharsets.UTF_8);
        var request = Json.parseObject(text);
        var result = WatermarkSession.runStandalone(request);
        System.out.println(Json.writePretty(result));
    }

    private WatermarkScriptRunner() {
    }
}
