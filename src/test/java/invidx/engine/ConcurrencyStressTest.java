package invidx.engine;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;

/**
 * Multiple writer threads (updates + deletes) race one reader thread that
 * never stops querying, with background merging enabled. Queries must
 * never throw and the final state must expose exactly one live revision
 * per non-deleted id, at its logical latest generation.
 */
class ConcurrencyStressTest {

    @Test
    void concurrentWritesReadsAndMerges(@TempDir Path tmp) throws Exception {
        IndexConfig cfg = new IndexConfig(4, true,
                java.time.Duration.ofMillis(20), 3);
        int idCount = 24;
        int writerCount = 4;
        int iterationsPerWriter = 120;

        // Logical truth per id, protected by the id's own monitor:
        // nextGen[id] = generation the next put receives;
        // liveGen[id] = generation of the live revision (0 if absent).
        long[] nextGen = new long[idCount];
        long[] liveGen = new long[idCount];
        Object[] locks = new Object[idCount];
        for (int i = 0; i < idCount; i++) {
            locks[i] = new Object();
        }
        AtomicInteger liveCount = new AtomicInteger();

        ExecutorService pool = Executors.newFixedThreadPool(writerCount + 1);
        CountDownLatch start = new CountDownLatch(1);
        List<Future<?>> futures = new ArrayList<>();

        try (InvertedIndex index = InvertedIndex.open(tmp, cfg, CrashHook.NOOP)) {
            AtomicInteger queryErrors = new AtomicInteger();
            futures.add(pool.submit(() -> {
                try {
                    start.await();
                    while (!Thread.currentThread().isInterrupted()) {
                        for (int id = 0; id < idCount; id++) {
                            index.search("term" + (id % 6));
                            index.search("OR:shared,term" + id);
                            index.search("AND:shared,term" + (id % 6));
                        }
                        index.snapshot();
                    }
                } catch (InterruptedException expected) {
                    Thread.currentThread().interrupt();
                } catch (RuntimeException e) {
                    queryErrors.incrementAndGet();
                }
            }));

            for (int w = 0; w < writerCount; w++) {
                final int writerId = w;
                futures.add(pool.submit(() -> {
                    start.await();
                    var rnd = new java.util.Random(0x9E3779B97F4A7C15L ^ writerId);
                    for (int i = 0; i < iterationsPerWriter; i++) {
                        int id = rnd.nextInt(idCount);
                        synchronized (locks[id]) {
                            long g = nextGen[id] == 0 ? 1 : nextGen[id];
                            nextGen[id] = g + 1;
                            boolean wasAbsent = liveGen[id] == 0;
                            liveGen[id] = g;
                            if (wasAbsent) {
                                liveCount.incrementAndGet();
                            }
                            index.putDocument(id,
                                    "term" + (id % 6) + " shared revision" + g);

                            if (rnd.nextInt(10) == 0) {
                                boolean removed = index.deleteDocument(id);
                                if (removed) {
                                    liveGen[id] = 0;
                                    liveCount.decrementAndGet();
                                }
                            }
                        }
                        if (rnd.nextInt(25) == 0) {
                            index.flush();
                        }
                    }
                    return null;
                }));
            }

            start.countDown();
            for (int i = 1; i < futures.size(); i++) {
                futures.get(i).get(2, TimeUnit.MINUTES);
            }
            pool.shutdownNow();
            pool.awaitTermination(5, TimeUnit.SECONDS);

            index.flush();
            index.forceMerge();

            assertEquals(0, queryErrors.get(), "reader observed a broken view");
            assertEquals(liveCount.get(), index.stats().liveDocCount());
            for (int id = 0; id < idCount; id++) {
                synchronized (locks[id]) {
                    if (liveGen[id] != 0) {
                        assertEquals(liveGen[id], index.get(id).gen(),
                                "id " + id + " exposes wrong generation");
                    }
                }
            }
        }
    }
}
