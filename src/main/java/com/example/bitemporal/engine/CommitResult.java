package com.example.bitemporal.engine;

import com.example.bitemporal.model.BitemporalRecord;

import java.util.List;

/** 一次事务提交的结果：事务时间戳与新写入的物理行。 */
public record CommitResult(java.time.LocalDate transactionDate, List<BitemporalRecord> writtenRows) {
}
