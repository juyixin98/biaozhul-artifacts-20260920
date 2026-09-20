package com.example.asset.web;

import com.example.asset.domain.Asset;
import com.example.asset.domain.DepreciationEntry;
import com.example.asset.repository.AssetRepository;
import com.example.asset.service.DepreciationService;
import com.example.asset.web.dto.DepreciationEntryResponse;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.*;

import java.time.YearMonth;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.stream.Collectors;

@Tag(name = "Depreciation", description = "折旧计提与明细导出")
@RestController
@RequestMapping("/api/depreciation")
public class DepreciationController {

    private final DepreciationService depreciationService;
    private final AssetRepository assetRepository;

    public DepreciationController(DepreciationService depreciationService, AssetRepository assetRepository) {
        this.depreciationService = depreciationService;
        this.assetRepository = assetRepository;
    }

    public record RunResponse(String period, int created, int skipped, List<DepreciationEntryResponse> entries) {
    }

    @Operation(summary = "执行某期间计提（财务）。按 (资产,期间) 唯一记账，重跑幂等；已关账期间返回 409。")
    @PostMapping("/run")
    @PreAuthorize("hasRole('FINANCE')")
    public RunResponse run(@RequestParam String period) {
        var result = depreciationService.run(parsePeriod(period));
        return new RunResponse(result.period(), result.created(), result.skipped(),
                result.entries().stream().map(DepreciationEntryResponse::of).toList());
    }

    @Operation(summary = "查询某期间计提明细（JSON）")
    @GetMapping("/entries")
    public List<DepreciationEntryResponse> entries(@RequestParam String period) {
        return depreciationService.entriesOfPeriod(parsePeriod(period)).stream()
                .map(DepreciationEntryResponse::of).toList();
    }

    @Operation(summary = "导出某期间计提明细（CSV）：期初、调整额、本期计提、期末，可逐行解释计算过程。")
    @GetMapping("/export")
    public ResponseEntity<String> export(@RequestParam String period) {
        YearMonth ym = parsePeriod(period);
        List<DepreciationEntry> entries = depreciationService.entriesOfPeriod(ym);
        Map<Long, Asset> assets = assetRepository.findAllById(
                        entries.stream().map(DepreciationEntry::getAssetId).toList())
                .stream().collect(Collectors.toMap(Asset::getId, Function.identity()));

        StringBuilder csv = new StringBuilder(
                "asset_code,asset_name,department,method,period,opening_value,adjustment_delta,depreciation_amount,closing_value\n");
        for (DepreciationEntry e : entries) {
            Asset a = assets.get(e.getAssetId());
            csv.append(escape(a != null ? a.getAssetCode() : "")).append(',')
                    .append(escape(a != null ? a.getName() : "")).append(',')
                    .append(escape(a != null ? a.getDepartment() : "")).append(',')
                    .append(e.getMethod()).append(',')
                    .append(e.getPeriod()).append(',')
                    .append(e.getOpeningValue()).append(',')
                    .append(e.getAdjustmentDelta()).append(',')
                    .append(e.getAmount()).append(',')
                    .append(e.getClosingValue()).append('\n');
        }
        return ResponseEntity.ok()
                .header(HttpHeaders.CONTENT_DISPOSITION,
                        "attachment; filename=\"depreciation-" + ym + ".csv\"")
                .contentType(MediaType.parseMediaType("text/csv; charset=UTF-8"))
                .body(csv.toString());
    }

    static YearMonth parsePeriod(String period) {
        try {
            return YearMonth.parse(period);
        } catch (Exception e) {
            throw new com.example.asset.service.ApiException.BadRequest(
                    "invalid period '" + period + "', expected format YYYY-MM");
        }
    }

    private static String escape(String s) {
        return s.contains(",") || s.contains("\"") ? "\"" + s.replace("\"", "\"\"") + "\"" : s;
    }
}
