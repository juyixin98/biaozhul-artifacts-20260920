package com.example.intervals.fixed;

import com.example.intervals.error.ErrorCode;
import com.example.intervals.error.IntervalException;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.io.InputStream;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Loads and serves the bundled, immutable fixed test datasets from the
 * classpath resource {@code data/datasets.json}. No network or filesystem
 * lookup is performed; the data ships inside the jar.
 */
public final class DatasetRepository {

    private static final String RESOURCE = "/data/datasets.json";

    private final Map<String, DatasetDto> byId;
    private final List<DatasetDto> all;

    private DatasetRepository(List<DatasetDto> datasets) {
        Map<String, DatasetDto> map = new LinkedHashMap<>();
        for (DatasetDto ds : datasets) {
            if (ds.id() == null || ds.id().isBlank()) {
                throw new IllegalStateException("dataset missing id in " + RESOURCE);
            }
            if (map.put(ds.id(), ds) != null) {
                throw new IllegalStateException("duplicate dataset id in " + RESOURCE + ": " + ds.id());
            }
        }
        this.byId = Map.copyOf(map);
        this.all = List.copyOf(datasets);
    }

    public static DatasetRepository loadDefault() {
        ObjectMapper mapper = new ObjectMapper();
        try (InputStream in = DatasetRepository.class.getResourceAsStream(RESOURCE)) {
            if (in == null) {
                throw new IllegalStateException("missing classpath resource " + RESOURCE);
            }
            DatasetCatalogDto catalog = mapper.readValue(in, DatasetCatalogDto.class);
            if (catalog == null || catalog.datasets() == null) {
                throw new IllegalStateException("malformed dataset catalog " + RESOURCE);
            }
            return new DatasetRepository(catalog.datasets());
        } catch (IOException e) {
            throw new IllegalStateException("cannot read " + RESOURCE, e);
        }
    }

    public List<DatasetDto> all() {
        return all;
    }

    public boolean exists(String id) {
        return byId.containsKey(id);
    }

    public DatasetDto require(String id) {
        DatasetDto ds = byId.get(id);
        if (ds == null) {
            throw new IntervalException(ErrorCode.UNKNOWN_DATASET,
                    "unknown dataset '" + id + "'; known datasets: " + byId.keySet());
        }
        return ds;
    }
}
