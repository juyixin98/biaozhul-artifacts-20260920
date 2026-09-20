package com.itasset.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.LocalDateTime;

/**
 * 折旧计提运行请求（仅追加）：requestId 唯一，保证重复请求幂等；
 * 与分录的唯一键 (asset_id, period) 构成双保险，重跑不会重复记账。
 */
@Entity
@Table(name = "depreciation_run")
public class DepreciationRun {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "request_id", nullable = false, unique = true, length = 64)
    private String requestId;

    @Column(name = "asset_id", nullable = false)
    private Long assetId;

    @Column(name = "from_period", nullable = false, length = 6)
    private String fromPeriod;

    @Column(name = "to_period", nullable = false, length = 6)
    private String toPeriod;

    @Column(name = "created_at", nullable = false)
    private LocalDateTime createdAt = LocalDateTime.now();

    protected DepreciationRun() {
    }

    public DepreciationRun(String requestId, Long assetId, String fromPeriod, String toPeriod) {
        this.requestId = requestId;
        this.assetId = assetId;
        this.fromPeriod = fromPeriod;
        this.toPeriod = toPeriod;
    }

    public Long getId() { return id; }
    public String getRequestId() { return requestId; }
    public Long getAssetId() { return assetId; }
    public String getFromPeriod() { return fromPeriod; }
    public String getToPeriod() { return toPeriod; }
    public LocalDateTime getCreatedAt() { return createdAt; }
}
