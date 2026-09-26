package com.example.eventorder;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.example.eventorder.model.OrderResponse;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class MainTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static String runCli(String[] args, String stdin) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        ByteArrayOutputStream err = new ByteArrayOutputStream();
        int code = Main.run(args,
                new ByteArrayInputStream(stdin.getBytes(StandardCharsets.UTF_8)),
                new PrintStream(out, true, StandardCharsets.UTF_8),
                new PrintStream(err, true, StandardCharsets.UTF_8));
        return code + "\n" + out;
    }

    @Test
    void readsRequestFromStdinAndWritesResponse() throws Exception {
        String result = runCli(new String[]{},
                "{\"events\":[{\"id\":\"a\"},{\"id\":\"b\"}],"
                        + "\"dependencies\":[{\"before\":\"a\",\"after\":\"b\"}]}");
        assertTrue(result.startsWith("0\n"), result);
        JsonNode response = MAPPER.readTree(result.substring(2));
        assertTrue(response.get("satisfiable").asBoolean());
        assertEquals(List.of("a", "b"),
                MAPPER.convertValue(response.get("order"),
                        MAPPER.getTypeFactory().constructCollectionType(List.class, String.class)));
        assertTrue(response.get("tzdbVersion").asText().matches("\\d{4}[a-z]"));
    }

    @Test
    void readsRequestFromFile(@TempDir Path dir) throws Exception {
        Path file = dir.resolve("req.json");
        Files.writeString(file, "{\"events\":[{\"id\":\"solo\"}]}");
        String result = runCli(new String[]{file.toString()}, "");
        assertTrue(result.startsWith("0\n"), result);
        JsonNode response = MAPPER.readTree(result.substring(2));
        assertEquals("solo", response.get("order").get(0).asText());
    }

    @Test
    void invalidRequestYieldsExitCode2AndErrorEnvelope() throws Exception {
        String result = runCli(new String[]{}, "{\"events\":[]}");
        assertTrue(result.startsWith("2\n"), result);
        JsonNode envelope = MAPPER.readTree(result.substring(2));
        assertTrue(envelope.get("error").asText().contains("at least one event"));
        assertTrue(envelope.has("tzdbVersion"));
    }

    @Test
    void unsatisfiableRequestStillExitsZero() throws Exception {
        String result = runCli(new String[]{},
                "{\"events\":[{\"id\":\"a\"},{\"id\":\"b\"}],"
                        + "\"dependencies\":[{\"before\":\"a\",\"after\":\"b\"},"
                        + "{\"before\":\"b\",\"after\":\"a\"}]}");
        assertTrue(result.startsWith("0\n"), result);
        JsonNode response = MAPPER.readTree(result.substring(2));
        assertTrue(!response.get("satisfiable").asBoolean());
        assertEquals("CYCLE", response.get("conflicts").get(0).get("kind").asText());
    }

    @Test
    void responseRoundTripsThroughJsonIO() throws Exception {
        String result = runCli(new String[]{},
                "{\"events\":[{\"id\":\"a\"},{\"id\":\"b\"}]}");
        OrderResponse parsed = new ObjectMapper()
                .findAndRegisterModules()
                .readValue(result.substring(2), OrderResponse.class);
        assertEquals(2, parsed.validOrderCount());
    }
}
