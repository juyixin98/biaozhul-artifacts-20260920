package com.example.hlc.sim;

import com.example.hlc.clock.VirtualClock;
import com.example.hlc.core.HlcTimestamp;
import com.example.hlc.core.HybridLogicalClock;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Deterministic multi-node HLC simulator for tests. Each node owns a
 * {@link VirtualClock} and a {@link HybridLogicalClock}; events are executed
 * in scripted order and the happens-before relation is derived from program
 * order plus send/receive edges.
 */
public final class HlcSimulation {

    /** A single event in the simulated execution. */
    public record SimEvent(String id, String node, HlcTimestamp timestamp) {
    }

    private final Map<String, HybridLogicalClock> clocks = new HashMap<>();
    private final Map<String, VirtualClock> virtualClocks = new HashMap<>();
    private final List<SimEvent> events = new ArrayList<>();
    private final Map<String, List<String>> programOrder = new HashMap<>();
    private final List<String[]> messageEdges = new ArrayList<>();

    public void addNode(String node, long initialWallMillis) {
        VirtualClock vc = new VirtualClock(initialWallMillis);
        virtualClocks.put(node, vc);
        clocks.put(node, HybridLogicalClock.builder(vc, node).build());
        programOrder.put(node, new ArrayList<>());
    }

    public VirtualClock clockOf(String node) {
        return virtualClocks.get(node);
    }

    /** Local event on a node. */
    public SimEvent local(String node, String eventId) {
        return record(node, eventId, clocks.get(node).tick());
    }

    /** Send + receive: tick on the sender, merge on the receiver. */
    public void send(String from, String to, String sendEventId, String receiveEventId) {
        SimEvent sent = record(from, sendEventId, clocks.get(from).tick());
        SimEvent received = record(to, receiveEventId, clocks.get(to).receive(sent.timestamp()));
        messageEdges.add(new String[]{sent.id(), received.id()});
    }

    private SimEvent record(String node, String eventId, HlcTimestamp ts) {
        SimEvent event = new SimEvent(eventId, node, ts);
        events.add(event);
        programOrder.get(node).add(eventId);
        return event;
    }

    public List<SimEvent> events() {
        return events;
    }

    public SimEvent event(String eventId) {
        return events.stream()
                .filter(e -> e.id().equals(eventId))
                .findFirst()
                .orElseThrow(() -> new IllegalArgumentException("unknown event " + eventId));
    }

    /**
     * True iff event {@code a} happens-before event {@code b}: reachable via
     * program-order and message edges (transitive closure computed on demand).
     */
    public boolean happensBefore(String a, String b) {
        Map<String, List<String>> adjacency = new LinkedHashMap<>();
        for (List<String> order : programOrder.values()) {
            for (int i = 0; i + 1 < order.size(); i++) {
                adjacency.computeIfAbsent(order.get(i), k -> new ArrayList<>()).add(order.get(i + 1));
            }
        }
        for (String[] edge : messageEdges) {
            adjacency.computeIfAbsent(edge[0], k -> new ArrayList<>()).add(edge[1]);
        }
        Set<String> visited = new HashSet<>();
        List<String> stack = new ArrayList<>();
        stack.add(a);
        while (!stack.isEmpty()) {
            String current = stack.remove(stack.size() - 1);
            if (current.equals(b)) {
                return true;
            }
            if (visited.add(current)) {
                stack.addAll(adjacency.getOrDefault(current, List.of()));
            }
        }
        return false;
    }
}
