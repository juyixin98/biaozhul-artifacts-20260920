package com.example.bitemporal.engine;

import com.example.bitemporal.model.ChangeRequest;

import java.time.LocalDate;
import java.util.List;

/** 提交日志中的一条：何时提交、几笔变更、各自摘要。 */
public record CommitLogEntry(LocalDate transactionDate, int changeCount, List<String> changes) {

    static String summarize(ChangeRequest req) {
        return req.mode() + " " + req.entityId()
                + " -> " + req.department() + "/" + req.role()
                + " valid " + req.validInterval();
    }
}
