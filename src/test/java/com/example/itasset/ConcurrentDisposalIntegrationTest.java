package com.example.itasset;

import com.example.itasset.support.ApiClient;
import org.junit.jupiter.api.Test;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Retire/dispose and depreciation posting run concurrently. Whatever order wins, the
 * resulting ledger must be internally consistent:
 * <ul>
 *   <li>no charge booked after the exit period,</li>
 *   <li>asset NBV equals the last entry's closing NBV,</li>
 *   <li>exactly one transition of each kind succeeds.</li>
 * </ul>
 */
class ConcurrentDisposalIntegrationTest extends AbstractIntegrationTest {


    private long activateAsset(String code) {
        Map<String, Object> asset = new LinkedHashMap<>();
        asset.put("assetCode", code);
        asset.put("name", code);
        asset.put("department", "IT");
        asset.put("cost", 60000);
        asset.put("salvageValue", 6000);
        asset.put("method", "SL");
        long id = api.post("/api/assets", "manager", "cc-create-" + code, asset).json.get("id").asLong();
        long v = api.get("/api/assets/" + id, "viewer").json.get("version").asLong();
        Map<String, Object> act = new LinkedHashMap<>();
        act.put("targetStatus", "IN_USE");
        act.put("expectedVersion", v);
        act.put("effectiveDate", "2031-01-10");
        act.put("usefulLifeMonths", 48);
        act.put("reason", "deploy");
        assertThat(api.post("/api/assets/" + id + "/transitions", "manager",
                "cc-act-" + code, act).status).isEqualTo(200);
        return id;
    }

    @Test
    void retirementAndMonthlyRun_canNeverProduceChargeBeyondExitPeriod() throws Exception {
        // Both operations target the same month boundary 2031-05. The month-end advisory
        // lock plus per-asset row locks serialize them; either order must leave a
        // consistent ledger.
        long id = activateAsset("CC-R-0");
        long version = api.get("/api/assets/" + id, "viewer").json.get("version").asLong();

        CountDownLatch start = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        AtomicReference<Integer> runStatus = new AtomicReference<>();
        AtomicReference<Integer> retireStatus = new AtomicReference<>();

        Future<?> runFuture = pool.submit(() -> {
            await(start);
            var resp = api.post("/api/depreciation/runs", "finance",
                    "cc-run-0", Map.of("period", "2031-05"));
            runStatus.set(resp.status);
        });
        Future<?> retireFuture = pool.submit(() -> {
            await(start);
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("targetStatus", "RETIRED");
            body.put("expectedVersion", version);
            body.put("effectivePeriod", "2031-05");
            body.put("reason", "concurrent retire");
            var resp = api.post("/api/assets/" + id + "/transitions", "manager",
                    "cc-retire-0", body);
            retireStatus.set(resp.status);
        });

        start.countDown();
        runFuture.get(40, TimeUnit.SECONDS);
        retireFuture.get(40, TimeUnit.SECONDS);
        pool.shutdown();

        assertThat(runStatus.get()).isEqualTo(200);
        // Posting updates NBV on the same row, which bumps the optimistic version: if the
        // run commits first, the in-flight retirement loses (409). It is still consistent -
        // the manager simply re-reads the version and retries successfully.
        if (retireStatus.get() == 409) {
            long fresh = api.get("/api/assets/" + id, "viewer").json.get("version").asLong();
            Map<String, Object> retry = new LinkedHashMap<>();
            retry.put("targetStatus", "RETIRED");
            retry.put("expectedVersion", fresh);
            retry.put("effectivePeriod", "2031-05");
            retry.put("reason", "concurrent retire retry");
            assertThat(api.post("/api/assets/" + id + "/transitions", "manager",
                    "cc-retire-0-retry", retry).status).isEqualTo(200);
        } else {
            assertThat(retireStatus.get()).isEqualTo(200);
        }

        // Whatever order committed, the May charge is allowed (当月减少，当月照提) and
        // nothing exists beyond the exit period.
        assertLedgerConsistent(id, "2031-05");
        assertThat(api.get("/api/assets/" + id, "viewer").json.get("status").asText())
                .isEqualTo("RETIRED");
    }

    @Test
    void twoConcurrentRetirements_onlyOneSucceeds() throws Exception {
        long id = activateAsset("CC-X");
        long version = api.get("/api/assets/" + id, "viewer").json.get("version").asLong();

        CountDownLatch start = new CountDownLatch(1);
        ExecutorService pool = Executors.newFixedThreadPool(2);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("targetStatus", "RETIRED");
        body.put("expectedVersion", version);
        body.put("effectivePeriod", "2031-04");
        body.put("reason", "race");

        Future<Integer> f1 = pool.submit(() -> {
            await(start);
            return api.post("/api/assets/" + id + "/transitions", "manager",
                    UUID.randomUUID().toString(), body).status;
        });
        Future<Integer> f2 = pool.submit(() -> {
            await(start);
            return api.post("/api/assets/" + id + "/transitions", "manager",
                    UUID.randomUUID().toString(), body).status;
        });
        start.countDown();
        int s1 = f1.get(30, TimeUnit.SECONDS);
        int s2 = f2.get(30, TimeUnit.SECONDS);
        pool.shutdown();

        // Exactly one writer commits; the order is non-deterministic.
        assertThat(java.util.List.of(s1, s2))
                .containsExactlyInAnyOrder(200, 409);
        assertThat(api.get("/api/assets/" + id, "viewer").json.get("status").asText())
                .isEqualTo("RETIRED");
        // Only one retirement record exists.
        var history = api.get("/api/assets/" + id + "/transitions", "viewer").json;
        long retires = java.util.stream.StreamSupport.stream(history.spliterator(), false)
                .filter(n -> n.get("toStatus").asText().equals("RETIRED"))
                .count();
        assertThat(retires).isEqualTo(1);
    }

    @Test
    void disposalAndRun_booksNoPostExitCharge_andNbvMatchesLastEntry() throws Exception {
        long id = activateAsset("CC-D");
        long v = api.get("/api/assets/" + id, "viewer").json.get("version").asLong();
        // Retire first at 2031-04
        Map<String, Object> retire = new LinkedHashMap<>();
        retire.put("targetStatus", "RETIRED");
        retire.put("expectedVersion", v);
        retire.put("effectivePeriod", "2031-04");
        retire.put("reason", "r");
        assertThat(api.post("/api/assets/" + id + "/transitions", "manager",
                "cc-d-retire", retire).status).isEqualTo(200);
        long v2 = api.get("/api/assets/" + id, "viewer").json.get("version").asLong();

        // Post through 2031-05. Posting updates NBV (bumps version); the retirement exit
        // (2031-04) stays authoritative, so only entries through 2031-04 are booked.
        assertThat(api.post("/api/depreciation/runs", "finance",
                "cc-d-run", Map.of("period", "2031-05")).status).isEqualTo(200);
        long v3 = api.get("/api/assets/" + id, "viewer").json.get("version").asLong();

        // Disposal cannot precede the retirement period (2031-04). Use the current version
        // so the period rule (422), not a stale-version conflict (409), is what is tested.
        Map<String, Object> earlyDispose = new LinkedHashMap<>();
        earlyDispose.put("targetStatus", "DISPOSED");
        earlyDispose.put("expectedVersion", v3);
        earlyDispose.put("effectivePeriod", "2031-03");
        earlyDispose.put("reason", "late disposal");
        assertThat(api.post("/api/assets/" + id + "/transitions", "manager",
                "cc-d-dispose-early", earlyDispose).status).isEqualTo(422);

        // Disposal at/after the retirement period is accepted and stays terminal
        Map<String, Object> dispose = new LinkedHashMap<>();
        dispose.put("targetStatus", "DISPOSED");
        dispose.put("expectedVersion", v3);
        dispose.put("effectivePeriod", "2031-05");
        dispose.put("reason", "sold");
        assertThat(api.post("/api/assets/" + id + "/transitions", "manager",
                "cc-d-dispose", dispose).status).isEqualTo(200);

        // The May charge was booked BEFORE retirement-exit took effect on the window? No:
        // exitPeriod was already 2031-04 from retirement, so the run books through 04 only.
        var lines = api.get("/api/assets/" + id + "/depreciation?throughPeriod=2031-12", "viewer")
                .json.get("lines");
        for (var line : lines) {
            if (line.get("period").asText().compareTo("2031-04") > 0) {
                assertThat(line.get("booked").asBoolean()).isFalse();
            }
        }
        assertLedgerConsistent(id, "2031-04");
    }

    private void await(CountDownLatch latch) {
        try {
            latch.await();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private void assertLedgerConsistent(long id, String exitPeriod) {
        var assetJson = api.get("/api/assets/" + id, "viewer").json;
        String nbv = assetJson.get("nbv").asText();

        var entries = api.get("/api/assets/" + id + "/depreciation?throughPeriod=2031-12", "viewer").json
                .get("lines");
        String lastBookedClosing = null;
        for (var line : entries) {
            String period = line.get("period").asText();
            boolean booked = line.get("booked").asBoolean();
            double charge = line.get("charge").asDouble();
            if (period.compareTo(exitPeriod) > 0) {
                // Nothing booked beyond the exit window
                assertThat(booked).isFalse();
            }
            if (booked) {
                assertThat(charge).isGreaterThanOrEqualTo(0);
                lastBookedClosing = line.get("closingNbv").asText();
            }
        }
        if (lastBookedClosing != null) {
            assertThat(nbv).isEqualTo(lastBookedClosing);
        }
        // NBV never below salvage
        assertThat(new java.math.BigDecimal(nbv))
                .isGreaterThanOrEqualTo(new java.math.BigDecimal(assetJson.get("salvageValue").asText()));
    }
}
