package com.example.timeout.core;

/** 超时条目状态。 */
public enum TimeoutStatus {
    /** 已安排，单调截止时刻尚未到达。 */
    SCHEDULED,
    /** 单调截止时刻已到（包括安排时墙钟截止时间已在过去、或停机期间到期在重启时被重算出来）。 */
    EXPIRED
}
