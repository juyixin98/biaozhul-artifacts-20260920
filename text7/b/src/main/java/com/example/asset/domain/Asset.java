package com.example.asset.domain;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;

/**
 * 硬件资产。金额一律使用定点数 decimal(19,2)。
 * version 为乐观锁字段；所有会改变账面/状态的写路径同时持有该行的悲观写锁，
 * 保证与折旧计提、退役/处置并发时只有一个成功。
 */
@Entity
@Table(name = "assets")
@Getter
@Setter
@NoArgsConstructor
public class Asset {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "asset_code", nullable = false, unique = true, length = 64)
    private String assetCode;

    @Column(nullable = false, length = 128)
    private String name;

    @Column(name = "purchase_cost", nullable = false, precision = 19, scale = 2)
    private BigDecimal purchaseCost;

    @Column(name = "salvage_value", nullable = false, precision = 19, scale = 2)
    private BigDecimal salvageValue;

    /** 启用日期；按“当月启用、次月计提”规则开始折旧。 */
    @Column(name = "commission_date", nullable = false)
    private LocalDate commissionDate;

    @Column(name = "useful_life_months", nullable = false)
    private int usefulLifeMonths;

    @Column(nullable = false, length = 64)
    private String department;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false, length = 16)
    private AssetStatus status = AssetStatus.IN_STOCK;

    @Enumerated(EnumType.STRING)
    @Column(name = "depreciation_method", nullable = false, length = 32)
    private DepreciationMethod depreciationMethod = DepreciationMethod.STRAIGHT_LINE;

    /**
     * 尚未入账的账面价值调整额（成本调整与未来适用法配套）：
     * 下一次计提时并入期初金额，随后清零。已有关账期间不受影响。
     */
    @Column(name = "pending_book_value_delta", nullable = false, precision = 19, scale = 2)
    private BigDecimal pendingBookValueDelta = BigDecimal.ZERO.setScale(2);

    @Version
    @Column(nullable = false)
    private long version;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt = Instant.now();

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt = Instant.now();

    @PreUpdate
    void touch() {
        this.updatedAt = Instant.now();
    }
}
