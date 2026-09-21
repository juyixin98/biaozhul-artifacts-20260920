package com.example.itasset.domain;

import jakarta.persistence.*;

import java.time.Instant;

/**
 * 状态变更流水：仅追加（append-only）。每次转换连同资产当前状态在同一事务中提交。
 * requestId 全局唯一，重复请求直接命中已存在记录（幂等）。
 */
@Entity
@Table(name = "status_transition")
public class StatusTransition {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "asset_id", nullable = false)
    private Long assetId;

    @Column(name = "request_id", nullable = false, unique = true, length = 100)
    private String requestId;

    @Enumerated(EnumType.STRING)
    @Column(name = "from_status", length = 24)
    private AssetStatus fromStatus;

    @Enumerated(EnumType.STRING)
    @Column(name = "to_status", nullable = false, length = 24)
    private AssetStatus toStatus;

    @Column(name = "expected_version", nullable = false)
    private long expectedVersion;

    @Column(length = 500)
    private String note;

    @Column(nullable = false, length = 64)
    private String operator;

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    protected StatusTransition() {
    }

    public StatusTransition(Long assetId, String requestId, AssetStatus fromStatus, AssetStatus toStatus,
                            long expectedVersion, String note, String operator, Instant createdAt) {
        this.assetId = assetId;
        this.requestId = requestId;
        this.fromStatus = fromStatus;
        this.toStatus = toStatus;
        this.expectedVersion = expectedVersion;
        this.note = note;
        this.operator = operator;
        this.createdAt = createdAt;
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

    public long getExpectedVersion() {
        return expectedVersion;
    }

    public String getNote() {
        return note;
    }

    public String getOperator() {
        return operator;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }
}
