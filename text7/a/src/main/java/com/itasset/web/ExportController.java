package com.itasset.web;

import com.itasset.service.ExportService;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * 导出每期折旧计算明细（JSON / CSV）。
 * 每行解释：期初账面价值 - 本月计提 = 期末账面价值。
 */
@RestController
@RequestMapping("/api/exports")
public class ExportController {

    private static final String[] CSV_HEADERS = {
            "assetCode", "assetName", "department", "period", "method", "monthlyRatePct",
            "openingBookValue", "depreciationAmount", "closingBookValue",
            "segmentMonthIndex", "costSnapshot", "lifeMonthsSnapshot", "explanation"};

    private final ExportService exports;

    public ExportController(ExportService exports) {
        this.exports = exports;
    }

    @GetMapping("/periods/{period}")
    public ResponseEntity<?> exportPeriod(@PathVariable String period,
                                          @RequestParam(required = false) Long assetId,
                                          @RequestParam(defaultValue = "json") String format) {
        List<ExportService.ExportRow> rows = exports.periodReport(period, assetId);
        if ("csv".equalsIgnoreCase(format)) {
            StringBuilder sb = new StringBuilder("﻿"); // UTF-8 BOM，Excel 友好
            sb.append(String.join(",", CSV_HEADERS)).append('\n');
            for (ExportService.ExportRow r : rows) {
                sb.append(String.join(",",
                        csv(r.assetCode()), csv(r.assetName()), csv(r.department()),
                        r.period(), r.method(), csv(r.monthlyRatePct()),
                        r.openingBookValue(), r.depreciationAmount(), r.closingBookValue(),
                        String.valueOf(r.segmentMonthIndex()),
                        r.costSnapshot(), String.valueOf(r.lifeMonthsSnapshot()),
                        csv(r.explanation()))).append('\n');
            }
            byte[] body = sb.toString().getBytes(StandardCharsets.UTF_8);
            return ResponseEntity.ok()
                    .header(HttpHeaders.CONTENT_DISPOSITION,
                            "attachment; filename=depreciation-" + period + ".csv")
                    .contentType(MediaType.parseMediaType("text/csv;charset=UTF-8"))
                    .body(body);
        }
        return ResponseEntity.ok(rows);
    }

    private static String csv(String v) {
        if (v == null || v.isEmpty()) {
            return "";
        }
        return '"' + v.replace("\"", "\"\"") + '"';
    }
}
