package com.example.itasset.web;

import com.example.itasset.service.ExportService;
import com.example.itasset.service.LedgerService;
import com.example.itasset.web.dto.DepreciationEntryResponse;
import com.example.itasset.web.dto.DepreciationExplanation;
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

/** Read-only depreciation ledger, explanation and CSV export. */
@RestController
@RequestMapping("/api")
public class LedgerController {

    private final LedgerService ledgerService;
    private final ExportService exportService;

    public LedgerController(LedgerService ledgerService, ExportService exportService) {
        this.ledgerService = ledgerService;
        this.exportService = exportService;
    }

    /** Booked entries of one period with asset codes. */
    @GetMapping("/depreciation/entries")
    public List<DepreciationEntryResponse> entries(@RequestParam String period) {
        var codes = ledgerService.assetCodeMap();
        return ledgerService.entriesForPeriod(period).stream()
                .map(e -> DepreciationEntryResponse.of(e, codes.get(e.getAssetId())))
                .toList();
    }

    /** Full opening/charge/closing explanation for one asset, booked plus projected. */
    @GetMapping("/assets/{id}/depreciation")
    public DepreciationExplanation explain(@PathVariable Long id,
                                           @RequestParam(required = false) String throughPeriod) {
        return ledgerService.explain(id, throughPeriod);
    }

    /** CSV export of the per-period calculation detail. */
    @GetMapping("/export/depreciation")
    public ResponseEntity<byte[]> exportCsv(@RequestParam String period) {
        String csv = exportService.exportPeriodCsv(period);
        String filename = "depreciation-" + period + ".csv";
        return ResponseEntity.ok()
                .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename=\"" + filename + "\"")
                .contentType(new MediaType("text", "csv", StandardCharsets.UTF_8))
                .body(csv.getBytes(StandardCharsets.UTF_8));
    }
}
