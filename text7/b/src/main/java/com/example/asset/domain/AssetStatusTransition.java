package com.example.asset.domain;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;

import java.time.Instant;

/**
 * 仅追加的状态变更记录。与资产当前状态在同一事务中提交。
 * requestId 全局唯一，用于幂等：重复请求返回已存在的记录，不产生第二条。
 */
@Entity
@Table(name = "asset_status_transitions")
@Getter
@NoArgsConstructor
public class AssetStatusTransition {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "asset_id", nullable = false)
    private Long assetId;

    @Enumerated(EnumType.STRING)
    @Column(name = "from_status", nullable = false, length = 16)
    private AssetStatus fromStatus;

    @Enumerated(EnumType.STRING)
    @Column(name = "to_status", nullable = false, length = 16)
    private AssetStatus toStatus;

    @Column(name = "request_id", nullable = false, unique = true, length = 64)
    private String requestId;

    @Column(name = "expected_version", nullable = false)
    private long expectedVersion;

    @Column(nullable = false, length = 64)
    private String actor;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt = Instant.now();

    public AssetStatusTransition(Long assetId, AssetStatus fromStatus, AssetStatus toStatus,
                                 String requestId, long expectedVersion, String actor) {
        this.assetId = assetId;
        this.fromStatus = fromStatus;
        this.toStatus = toStatus;
        this.requestId = requestId;
        this.expectedVersion = expectedVersion;
        this.actor = actor;
    }
}
