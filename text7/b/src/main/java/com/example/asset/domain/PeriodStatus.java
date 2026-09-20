package com.example.asset.domain;

/** 会计期间状态：OPEN 可计提/调整，CLOSED 已关账不可重算。 */
public enum PeriodStatus {
    OPEN,
    CLOSED
}
