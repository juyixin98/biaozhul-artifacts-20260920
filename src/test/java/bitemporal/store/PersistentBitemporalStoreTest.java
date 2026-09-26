package bitemporal.store;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import bitemporal.model.ChangeRequest;
import bitemporal.model.QueryRequest;
import bitemporal.model.TemporalRecord;
import bitemporal.model.TransactionRequest;

import java.nio.file.Path;
import java.time.Instant;
import java.util.List;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class PersistentBitemporalStoreTest {

    @TempDir
    Path tempDir;

    @Test
    void stateSurvivesAcrossProcessLikeInstances() {
        Path db = tempDir.resolve("state/bitemporal-db.json");

        // 第一个“进程”：种子 + 追溯修订。
        PersistentBitemporalStore first = new PersistentBitemporalStore(db);
        first.commit(SeedData.seedTransaction());
        first.commit(new TransactionRequest("fix", Instant.parse("2026-03-01T09:00:00Z"),
                List.of(new ChangeRequest("revise", "emp-1001", "CORRECTED",
                        Instant.parse("2026-01-15T00:00:00Z"),
                        Instant.parse("2026-03-01T00:00:00Z")))));

        // 第二个“进程”：打开同一文件应看到修订后结果与历史。
        PersistentBitemporalStore second = new PersistentBitemporalStore(db);
        List<TemporalRecord> corrected = second.asOf(new QueryRequest(null,
                Instant.parse("2026-02-15T00:00:00Z"),
                Instant.parse("2026-03-02T00:00:00Z"), "emp-1001"));
        assertEquals(1, corrected.size());
        assertEquals("CORRECTED", corrected.get(0).data());
        assertEquals(Instant.parse("2026-03-01T09:00:00Z"), second.lastCommitAt().orElseThrow());
        assertTrue(db.toFile().length() > 0);

        // reset 后第三个实例看到空库。
        second.reset();
        PersistentBitemporalStore third = new PersistentBitemporalStore(db);
        assertTrue(third.recordIds().isEmpty());
        assertTrue(third.lastCommitAt().isEmpty());
    }
}
