package com.example.itasset.web;

import com.example.itasset.domain.Asset;
import com.example.itasset.domain.DepreciationEntry;
import com.example.itasset.repo.AssetRepository;
import com.example.itasset.repo.DepreciationEntryRepository;
import com.example.itasset.service.AssetLifecycleService;
import com.example.itasset.support.Periods;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * 导出每期折旧计算明细（CSV）。每行解释 期初金额、本月计提、期末金额 及计算口径；
 * UTF-8 BOM 便于 Excel 直接打开。任何已认证角色可导出（财务为主要使用方）。
 */
@RestController
@RequestMapping("/api/export")
public class ExportController {

    private final DepreciationEntryRepository entries;
    private final AssetRepository assets;
    private final AssetLifecycleService lifecycle;

    public ExportController(DepreciationEntryRepository entries,
                            AssetRepository assets,
                            AssetLifecycleService lifecycle) {
        this.entries = entries;
        this.assets = assets;
        this.lifecycle = lifecycle;
    }

    @GetMapping(value = "/depreciation/{period}", produces = "text/csv;charset=UTF-8")
    public ResponseEntity<byte[]> periodCsv(@PathVariable Integer period) {
        List<DepreciationEntry> rows = entries.findByPeriodOrderByAssetIdAsc(period);
        StringBuilder sb = new StringBuilder("﻿"); // UTF-8 BOM
        sb.append("期间,资产ID,资产编号,资产名称,部门,折旧方法,期初账面价值,本月计提,期末账面价值,政策版本ID,计算说明\r\n");
        BigDecimal sumOpening = BigDecimal.ZERO;
        BigDecimal sumCharge = BigDecimal.ZERO;
        BigDecimal sumClosing = BigDecimal.ZERO;
        for (DepreciationEntry e : rows) {
            Asset a = assets.findById(e.getAssetId()).orElseThrow();
            sb.append(Periods.format(e.getPeriod())).append(',')
                    .append(a.getId()).append(',')
                    .append(csv(a.getAssetCode())).append(',')
                    .append(csv(a.getName())).append(',')
                    .append(csv(a.getDepartment())).append(',')
                    .append(a.getDepreciationMethod()).append(',')
                    .append(e.getOpeningValue()).append(',')
                    .append(e.getCharge()).append(',')
                    .append(e.getClosingValue()).append(',')
                    .append(e.getPolicyId()).append(',')
                    .append(csv(e.getCalcDetail())).append("\r\n");
            sumOpening = sumOpening.add(e.getOpeningValue());
            sumCharge = sumCharge.add(e.getCharge());
            sumClosing = sumClosing.add(e.getClosingValue());
        }
        sb.append("合计,,,,,,").append(sumOpening).append(',')
                .append(sumCharge).append(',').append(sumClosing).append(",,\r\n");

        byte[] body = sb.toString().getBytes(StandardCharsets.UTF_8);
        return ResponseEntity.ok()
                .header(HttpHeaders.CONTENT_DISPOSITION,
                        "attachment; filename=depreciation-" + period + ".csv")
                .contentType(MediaType.parseMediaType("text/csv;charset=UTF-8"))
                .body(body);
    }

    /**
     * 资产维度 JSON 说明：列出全部已记账期间的 期初/计提/期末，前端可直接解释账龄。
     */
    @GetMapping("/assets/{id}/depreciation")
    public List<Views.EntryView> assetEntries(@PathVariable Long id) {
        lifecycle.requireAsset(id);
        return entries.findByAssetIdOrderByPeriodAsc(id).stream()
                .map(Views.EntryView::of).toList();
    }

    private static String csv(String value) {
        if (value == null) {
            return "";
        }
        if (value.contains(",") || value.contains("\"") || value.contains("\n")) {
            return '"' + value.replace("\"", "\"\"") + '"';
        }
        return value;
    }
}
