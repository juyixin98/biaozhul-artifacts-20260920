package com.example.itasset.domain;

/**
 * 资产状态及允许的转换。
 *
 * <pre>
 * IN_STOCK  库存      -> IN_USE
 * IN_USE    使用中    -> IN_REPAIR | RETIRED
 * IN_REPAIR 维修      -> IN_USE | RETIRED
 * RETIRED   退役      -> DISPOSED            （退役后不可恢复使用）
 * DISPOSED  处置      -> （终态，无任何转出）
 * </pre>
 */
public enum AssetStatus {
    IN_STOCK,
    IN_USE,
    IN_REPAIR,
    RETIRED,
    DISPOSED;

    public boolean canTransitionTo(AssetStatus target) {
        return switch (this) {
            case IN_STOCK -> target == IN_USE;
            case IN_USE -> target == IN_REPAIR || target == RETIRED;
            case IN_REPAIR -> target == IN_USE || target == RETIRED;
            case RETIRED -> target == DISPOSED;
            case DISPOSED -> false;
        };
    }
}
