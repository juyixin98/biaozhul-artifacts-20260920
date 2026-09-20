package com.itasset.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.LocalDateTime;

/**
 * 资产状态转换记录（仅追加）。
 *
 * <p>由 {@code trg_status_change_no_update/no_delete} 触发器在数据库层
 * 禁止 UPDATE/DELETE；与资产当前状态在同一事务内插入。
 * {@code request_id} 建唯一索引，保证重复请求幂等。
 */
@Entity
@Table(name = "status_change")
public class StatusChange {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "asset_id", nullable = false)
    private Long assetId;

    @Enumerated(EnumType.STRING)
    @Column(name = "from_status", length = 32)
    private AssetStatus fromStatus;

    @Enumerated(EnumType.STRING)
    @Column(name = "to_status", nullable = false, length = 32)
    private AssetStatus toStatus;

    /** 客户端提供的预期乐观锁版本。 */
    @Column(name = "expected_version", nullable = false)
    private Long expectedVersion;

    /** 幂等请求 ID（同一 requestId 重试返回首次结果，不重复记账）。 */
    @Column(name = "request_id", nullable = false, length = 64)
    private String requestId;

    @Column(length = 500)
    private String reason;

    @Column(name = "changed_by", nullable = false, length = 100)
    private String changedBy;

    @Column(name = "created_at", nullable = false)
    private LocalDateTime createdAt = LocalDateTime.now();

    protected StatusChange() {
    }

    public StatusChange(Long assetId, AssetStatus fromStatus, AssetStatus toStatus,
                        Long expectedVersion, String requestId, String reason, String changedBy) {
        this.assetId = assetId;
        this.fromStatus = fromStatus;
        this.toStatus = toStatus;
        this.expectedVersion = expectedVersion;
        this.requestId = requestId;
        this.reason = reason;
        this.changedBy = changedBy;
    }

    public Long getId() { return id; }
    public Long getAssetId() { return assetId; }
    public AssetStatus getFromStatus() { return fromStatus; }
    public AssetStatus getToStatus() { return toStatus; }
    public Long getExpectedVersion() { return expectedVersion; }
    public String getRequestId() { return requestId; }
    public String getReason() { return reason; }
    public String getChangedBy() { return changedBy; }
    public LocalDateTime getCreatedAt() { return createdAt; }
}
