package com.example.itasset.service;

import com.example.itasset.domain.DepreciationEntry;
import com.example.itasset.domain.HardwareAsset;
import com.example.itasset.repo.DepreciationEntryRepository;
import com.example.itasset.repo.HardwareAssetRepository;
import com.example.itasset.support.Periods;
import com.example.itasset.web.ApiException;
import com.example.itasset.web.dto.DepreciationExplanation;
import com.example.itasset.web.dto.DepreciationLine;
import org.springframework.stereotype.Service;
import org.springframework.data.domain.Sort;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.time.YearMonth;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Read-side reporting. Explains, for every period of an asset's life, the opening NBV,
 * the charge and the closing NBV. Booked periods come straight from the append-only
 * ledger; periods after {@code lastPostedPeriod} are projected with the current regime
 * parameters so finance can see the future run-off.
 */
@Service
public class LedgerService {

    private final HardwareAssetRepository assetRepository;
    private final DepreciationEntryRepository entryRepository;

    public LedgerService(HardwareAssetRepository assetRepository,
                         DepreciationEntryRepository entryRepository) {
        this.assetRepository = assetRepository;
        this.entryRepository = entryRepository;
    }

    @Transactional(readOnly = true)
    public List<DepreciationEntry> entriesForPeriod(String period) {
        Periods.parse(period);
        return entryRepository.findByPeriodOrderByAssetIdAsc(period);
    }

    @Transactional(readOnly = true)
    public Map<Long, String> assetCodeMap() {
        Map<Long, String> codes = new LinkedHashMap<>();
        for (HardwareAsset a : assetRepository.findAll(Sort.by("id"))) {
            codes.put(a.getId(), a.getAssetCode());
        }
        return codes;
    }

    @Transactional(readOnly = true)
    public DepreciationExplanation explain(Long assetId, String throughPeriod) {
        HardwareAsset asset = assetRepository.findById(assetId)
                .orElseThrow(() -> ApiException.notFound("Asset not found: " + assetId));
        List<DepreciationEntry> booked = entryRepository.findByAssetIdOrderByPeriodAsc(assetId);

        String firstEligible = asset.getInServiceDate() == null
                ? null
                : Periods.firstDepreciationPeriod(asset.getInServiceDate());

        YearMonth end = resolveEnd(asset, firstEligible, throughPeriod);

        Map<String, DepreciationEntry> bookedByPeriod = new LinkedHashMap<>();
        for (DepreciationEntry e : booked) {
            bookedByPeriod.put(e.getPeriod(), e);
        }

        List<DepreciationLine> lines = new ArrayList<>();
        if (firstEligible != null) {
            BigDecimal nbv = asset.getCost();
            int usedInRegime = 0;
            YearMonth cursor = Periods.parse(firstEligible);
            while (!cursor.isAfter(end)) {
                String p = Periods.of(cursor);
                DepreciationEntry stored = bookedByPeriod.get(p);
                BigDecimal opening;
                BigDecimal charge;
                BigDecimal closing;
                String note = null;
                boolean eligible;
                boolean isBooked = stored != null;

                if (stored != null) {
                    opening = stored.getOpeningNbv();
                    charge = stored.getCharge();
                    closing = stored.getClosingNbv();
                    eligible = true; // booked entries were eligible by construction
                    nbv = closing;
                    usedInRegime++;
                } else {
                    opening = nbv;
                    eligible = isEligible(asset, firstEligible, p, usedInRegime);
                    if (eligible) {
                        int remaining = asset.getUsefulLifeMonths() - usedInRegime;
                        charge = DepreciationPolicy.monthlyCharge(
                                asset.getMethod(), opening, asset.getSalvageValue(),
                                asset.getSlMonthly(), asset.getDdbRate(), remaining);
                        usedInRegime++;
                        if (charge.signum() == 0) {
                            note = "charge rounds to zero at salvage floor";
                        }
                    } else {
                        charge = BigDecimal.ZERO.setScale(DepreciationPolicy.MONEY_SCALE);
                        if (asset.getExitPeriod() != null && p.compareTo(asset.getExitPeriod()) > 0) {
                            note = "after exit period";
                        }
                    }
                    closing = opening.subtract(charge);
                    nbv = closing;
                }
                lines.add(new DepreciationLine(p, asset.getMethod().name(), opening, charge,
                        closing, eligible, isBooked, note));
                cursor = cursor.plusMonths(1);
            }
        }

        return new DepreciationExplanation(
                asset.getId(),
                asset.getAssetCode(),
                asset.getStatus().name(),
                asset.getMethod().name(),
                asset.getCost(),
                asset.getSalvageValue(),
                asset.getUsefulLifeMonths(),
                asset.getInServiceDate() == null ? null : Periods.of(asset.getInServiceDate()),
                firstEligible,
                asset.getExitPeriod(),
                asset.getLastPostedPeriod(),
                asset.getNbv(),
                asset.getUsedMonths(),
                asset.getSlMonthly(),
                asset.getDdbRate(),
                lines);
    }

    private YearMonth resolveEnd(HardwareAsset asset, String firstEligible, String throughPeriod) {
        if (throughPeriod != null) {
            return Periods.parse(throughPeriod);
        }
        if (asset.getExitPeriod() != null) {
            return Periods.parse(asset.getExitPeriod());
        }
        if (firstEligible != null && asset.getUsefulLifeMonths() != null) {
            return Periods.parse(firstEligible).plusMonths(asset.getUsefulLifeMonths() - 1L);
        }
        return YearMonth.now().plusMonths(12);
    }

    private boolean isEligible(HardwareAsset asset, String firstEligible, String period, int usedInRegime) {
        if (firstEligible == null || period.compareTo(firstEligible) < 0) {
            return false;
        }
        if (asset.getExitPeriod() != null && period.compareTo(asset.getExitPeriod()) > 0) {
            return false;
        }
        return usedInRegime < asset.getUsefulLifeMonths();
    }
}
