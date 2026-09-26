package com.example.intervals.fixed;

import com.example.intervals.error.ErrorCode;
import com.example.intervals.error.IntervalException;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class DatasetRepositoryTest {

    private final DatasetRepository repo = DatasetRepository.loadDefault();

    @Test
    void loadsBundledDatasets() {
        assertTrue(repo.all().size() >= 4);
        assertTrue(repo.exists("time-adjacency"));
        assertTrue(repo.exists("time-dst-europe-2024"));
        assertTrue(repo.exists("time-unbounded"));
        assertTrue(repo.exists("version-rollout"));
    }

    @Test
    void everyDatasetHasSetsAndDomain() {
        for (DatasetDto ds : repo.all()) {
            assertEquals(true, ds.sets() != null && !ds.sets().isEmpty(),
                    "dataset " + ds.id() + " should define at least one set");
            assertTrue(ds.domain().equals("time") || ds.domain().equals("version"),
                    "unexpected domain for " + ds.id());
        }
    }

    @Test
    void unknownDatasetIsRejected() {
        IntervalException e = assertThrows(IntervalException.class, () -> repo.require("nope"));
        assertEquals(ErrorCode.UNKNOWN_DATASET, e.errorCode());
    }
}
