package com.example.itasset.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.LocalDateTime;

/** Append-only audit row for one state transition. Never updated or deleted. */
@Entity
@Table(name = "asset_transition")
public class AssetTransition {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "asset_id", nullable = false)
    private Long assetId;

    @Column(name = "request_id", nullable = false, unique = true, length = 64)
    private String requestId;

    @Enumerated(EnumType.STRING)
    @Column(name = "from_status", length = 20)
    private AssetStatus fromStatus;

    @Enumerated(EnumType.STRING)
    @Column(name = "to_status", nullable = false, length = 20)
    private AssetStatus toStatus;

    @Column(name = "effective_period", nullable = false, length = 7)
    private String effectivePeriod;

    @Column(length = 500)
    private String reason;

    @Column(name = "expected_version", nullable = false)
    private long expectedVersion;

    @Column(name = "resulted_version", nullable = false)
    private long resultedVersion;

    @Column(name = "created_at", nullable = false)
    private LocalDateTime createdAt;

    public AssetTransition() {
    }

    public AssetTransition(Long assetId, String requestId, AssetStatus fromStatus, AssetStatus toStatus,
                           String effectivePeriod, String reason, long expectedVersion, long resultedVersion) {
        this.assetId = assetId;
        this.requestId = requestId;
        this.fromStatus = fromStatus;
        this.toStatus = toStatus;
        this.effectivePeriod = effectivePeriod;
        this.reason = reason;
        this.expectedVersion = expectedVersion;
        this.resultedVersion = resultedVersion;
        this.createdAt = LocalDateTime.now();
    }

    public Long getId() {
        return id;
    }

    public Long getAssetId() {
        return assetId;
    }

    public String getRequestId() {
        return requestId;
    }

    public AssetStatus getFromStatus() {
        return fromStatus;
    }

    public AssetStatus getToStatus() {
        return toStatus;
    }

    public String getEffectivePeriod() {
        return effectivePeriod;
    }

    public String getReason() {
        return reason;
    }

    public long getExpectedVersion() {
        return expectedVersion;
    }

    public long getResultedVersion() {
        return resultedVersion;
    }

    public LocalDateTime getCreatedAt() {
        return createdAt;
    }
}
