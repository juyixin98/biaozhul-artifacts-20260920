package com.example.itasset.support;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.client.HttpClientErrorException;
import org.springframework.web.client.RestTemplate;

import java.nio.charset.StandardCharsets;
import java.util.Base64;

/** Minimal HTTP client for tests: JSON + basic auth + X-Request-Id. */
public class ApiClient {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final RestTemplate rest = new RestTemplate();
    private final String baseUrl;

    public ApiClient(String baseUrl) {
        this.baseUrl = baseUrl;
    }

    public static class Response {
        public final int status;
        public final String body;
        public final JsonNode json;

        Response(ResponseEntity<String> entity) {
            this.status = entity.getStatusCode().value();
            this.body = entity.getBody() == null ? "" : entity.getBody();
            this.json = parse(body);
        }

        Response(HttpClientErrorException ex) {
            this.status = ex.getStatusCode().value();
            this.body = ex.getResponseBodyAsString();
            this.json = parse(body);
        }

        private static JsonNode parse(String body) {
            try {
                return MAPPER.readTree(body);
            } catch (Exception e) {
                return null;
            }
        }
    }

    public Response get(String path, String user) {
        return exchange(HttpMethod.GET, path, user, null, null);
    }

    public Response post(String path, String user, String requestId, Object body) {
        return exchange(HttpMethod.POST, path, user, requestId, body);
    }

    public Response exchange(HttpMethod method, String path, String user, String requestId, Object body) {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        if (user != null) {
            String token = Base64.getEncoder()
                    .encodeToString((user + ":Pass#2026").getBytes(StandardCharsets.UTF_8));
            headers.set(HttpHeaders.AUTHORIZATION, "Basic " + token);
        }
        if (requestId != null) {
            headers.set("X-Request-Id", requestId);
        }
        String json = null;
        if (body != null) {
            try {
                json = body instanceof String s ? s : MAPPER.writeValueAsString(body);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        }
        try {
            return new Response(rest.exchange(baseUrl + path, method,
                    new HttpEntity<>(json, headers), String.class));
        } catch (HttpClientErrorException e) {
            return new Response(e);
        }
    }
}
