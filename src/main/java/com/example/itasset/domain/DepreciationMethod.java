package com.example.itasset.domain;

/**
 * 折旧方法。
 * <ul>
 *   <li>STRAIGHT_LINE：直线法，月计提 =（生效期初账面价值 − 残值）/ 剩余月数，最后一个月补差至残值。</li>
 *   <li>DECLINING_BALANCE：余额递减法（双倍余额递减），月率 = 2 / 原预计使用月数，
 *       按月初账面净值计提，最后一个月补差至残值；账面价值永不低于残值。</li>
 * </ul>
 */
public enum DepreciationMethod {
    STRAIGHT_LINE,
    DECLINING_BALANCE
}
