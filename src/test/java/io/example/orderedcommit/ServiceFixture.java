package io.example.orderedcommit;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Direct in-process handle to a service, for core-level tests. */
final class ServiceFixture implements AutoCloseable {

    final OrderedEventService service;

    private ServiceFixture(OrderedEventService service) {
        this.service = service;
    }

    static ServiceFixture start(int inflightCap, long bufferCap) {
        OrderedEventService.Config config =
                new OrderedEventService.Config(inflightCap, bufferCap, 50, 10_000, 1, 10);
        return new ServiceFixture(new OrderedEventService(config, new SleepingEventProcessor()));
    }

    Map<String, Object> submit(String partition, String spec) {
        return service.submit(partition, Json.readObject(spec));
    }

    Map<String, Object> submit(
            String partition,
            long delay,
            boolean fail,
            Long timeout,
            Integer maxAttempts,
            Long retryDelay) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("delayMillis", delay);
        body.put("fail", fail);
        if (timeout != null) {
            body.put("timeoutMillis", timeout);
        }
        if (maxAttempts != null) {
            body.put("maxAttempts", maxAttempts);
        }
        if (retryDelay != null) {
            body.put("retryDelayMillis", retryDelay);
        }
        return service.submit(partition, body);
    }

    @SuppressWarnings("unchecked")
    static long seq(Map<String, Object> view) {
        return ((Number) view.get("seq")).longValue();
    }

    @SuppressWarnings("unchecked")
    static String id(Map<String, Object> view) {
        return (String) view.get("id");
    }

    @SuppressWarnings("unchecked")
    static String status(Map<String, Object> view) {
        return (String) view.get("status");
    }

    @SuppressWarnings("unchecked")
    static long attempts(Map<String, Object> view) {
        return ((Number) view.get("attempts")).longValue();
    }

    List<Map<String, Object>> committed(String partition) throws InterruptedException {
        return service.results(partition, -1, 0);
    }

    @SuppressWarnings("unchecked")
    static long committedSeq(Map<String, Object> entry) {
        return ((Number) entry.get("seq")).longValue();
    }

    @SuppressWarnings("unchecked")
    static String outcome(Map<String, Object> entry) {
        return (String) entry.get("outcome");
    }

    @Override
    public void close() {
        service.shutdown();
    }
}
