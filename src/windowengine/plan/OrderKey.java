package windowengine.plan;

import windowengine.EngineException;
import windowengine.ErrorCode;

/**
 * 单个 ORDER BY 键：列名 + 方向 + NULL 位置。
 */
public record OrderKey(String column, Direction direction, NullOrder nullOrder) {

    public OrderKey {
        if (column == null || column.isBlank()) {
            throw new EngineException(ErrorCode.INVALID_REQUEST, "排序列名不能为空");
        }
        direction = direction == null ? Direction.ASC : direction;
        nullOrder = nullOrder == null ? NullOrder.defaultValue(direction) : nullOrder;
    }
}
