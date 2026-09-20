package com.itasset.service;

import com.itasset.domain.Asset;
import com.itasset.domain.DepreciationEntry;
import com.itasset.repo.AssetRepository;
import com.itasset.repo.DepreciationEntryRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.stream.Collectors;

/**
 * 导出每期折旧计算明细：期初账面价值、本月计提、期末账面价值，
 * 并附方法、段内月序号与参数快照，使每一行金额都可被解释和复核。
 */
@Service
public class ExportService {

    private final DepreciationEntryRepository entries;
    private final AssetRepository assets;

    public ExportService(DepreciationEntryRepository entries, AssetRepository assets) {
        this.entries = entries;
        this.assets = assets;
    }

    public record ExportRow(String assetCode, String assetName, String department,
                            String period, String method, String monthlyRatePct,
                            String openingBookValue, String depreciationAmount,
                            String closingBookValue, int segmentMonthIndex,
                            String costSnapshot, int lifeMonthsSnapshot,
                            String explanation) {
    }

    @Transactional(readOnly = true)
    public List<ExportRow> periodReport(String period, Long assetId) {
        List<DepreciationEntry> list = entries.findByPeriodOrderByAssetIdAsc(period).stream()
                .filter(e -> assetId == null || e.getAssetId().equals(assetId))
                .toList();
        Map<Long, Asset> assetMap = assets.findAllById(
                        list.stream().map(DepreciationEntry::getAssetId).distinct().toList())
                .stream().collect(Collectors.toMap(Asset::getId, Function.identity()));

        return list.stream().map(e -> {
            Asset a = assetMap.get(e.getAssetId());
            String explanation = explain(e);
            return new ExportRow(
                    a == null ? String.valueOf(e.getAssetId()) : a.getAssetCode(),
                    a == null ? "" : a.getName(),
                    a == null ? "" : a.getDepartment(),
                    e.getPeriod(), e.getDepreciationMethod().name(),
                    e.getMonthlyRatePct() == null ? "" : e.getMonthlyRatePct().toPlainString(),
                    e.getOpeningBookValue().toPlainString(),
                    e.getDepreciationAmount().toPlainString(),
                    e.getClosingBookValue().toPlainString(),
                    e.getPeriodIndex(),
                    e.getCostSnapshot().toPlainString(),
                    e.getLifeMonthsSnapshot(),
                    explanation);
        }).toList();
    }

    /** 生成人类可读的金额解释。 */
    private String explain(DepreciationEntry e) {
        String base = "期初账面价值 " + e.getOpeningBookValue().toPlainString()
                + " - 本月计提 " + e.getDepreciationAmount().toPlainString()
                + " = 期末账面价值 " + e.getClosingBookValue().toPlainString();
        String method;
        if (e.getDepreciationMethod() == com.itasset.domain.DepreciationMethod.STRAIGHT_LINE) {
            method = "直线法：可折旧基数（成本-残值）在使用月数内平摊，月计提四舍五入到分，末月补差到残值";
        } else {
            method = "余额递减法：以期初账面价值按月折旧率（年率/12="
                    + (e.getMonthlyRatePct() == null ? "" : e.getMonthlyRatePct().toPlainString())
                    + "%）计提，四舍五入到分，末月补差到残值";
        }
        return method + "；" + base + "（折旧段第 " + e.getPeriodIndex() + " 月，参数快照成本 "
                + e.getCostSnapshot().toPlainString() + "、使用月数 " + e.getLifeMonthsSnapshot() + "）";
    }
}
