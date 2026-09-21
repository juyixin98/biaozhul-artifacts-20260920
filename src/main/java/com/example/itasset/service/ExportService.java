package com.example.itasset.service;

import com.example.itasset.domain.DepreciationEntry;
import com.example.itasset.domain.HardwareAsset;
import com.example.itasset.repo.DepreciationEntryRepository;
import com.example.itasset.repo.HardwareAssetRepository;
import com.example.itasset.support.Periods;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.stream.Collectors;

/**
 * CSV export of the per-period calculation detail. Fixed-point amounts are printed with
 * exactly two decimals; the file begins with a UTF-8 BOM so Excel opens Chinese names
 * correctly.
 */
@Service
public class ExportService {

    public static final String BOM = "﻿";
    private static final String HEADER =
            "period,assetId,assetCode,name,department,status,method,openingNbv,charge,closingNbv\n";

    private final HardwareAssetRepository assetRepository;
    private final DepreciationEntryRepository entryRepository;

    public ExportService(HardwareAssetRepository assetRepository,
                         DepreciationEntryRepository entryRepository) {
        this.assetRepository = assetRepository;
        this.entryRepository = entryRepository;
    }

    @Transactional(readOnly = true)
    public String exportPeriodCsv(String period) {
        Periods.parse(period);
        List<DepreciationEntry> entries = entryRepository.findByPeriodOrderByAssetIdAsc(period);
        Map<Long, HardwareAsset> assets = assetRepository.findAll().stream()
                .collect(Collectors.toMap(HardwareAsset::getId, Function.identity()));

        StringBuilder sb = new StringBuilder();
        sb.append(BOM).append(HEADER);
        BigDecimal total = BigDecimal.ZERO;
        for (DepreciationEntry e : entries) {
            HardwareAsset a = assets.get(e.getAssetId());
            String code = a == null ? "" : a.getAssetCode();
            String name = a == null ? "" : csv(a.getName());
            String dept = a == null ? "" : csv(a.getDepartment());
            String status = a == null ? "" : a.getStatus().name();
            sb.append(e.getPeriod()).append(',')
                    .append(e.getAssetId()).append(',')
                    .append(csv(code)).append(',')
                    .append(name).append(',')
                    .append(dept).append(',')
                    .append(status).append(',')
                    .append(e.getMethod().name()).append(',')
                    .append(money(e.getOpeningNbv())).append(',')
                    .append(money(e.getCharge())).append(',')
                    .append(money(e.getClosingNbv())).append('\n');
            total = total.add(e.getCharge());
        }
        sb.append(period).append(",,,,,,,,").append(money(total)).append('\n');
        return sb.toString();
    }

    private static String money(BigDecimal v) {
        return v.setScale(2).toPlainString();
    }

    private static String csv(String raw) {
        if (raw == null) {
            return "";
        }
        if (raw.contains(",") || raw.contains("\"") || raw.contains("\n")) {
            return '"' + raw.replace("\"", "\"\"") + '"';
        }
        return raw;
    }
}
