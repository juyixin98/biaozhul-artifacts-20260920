package com.example.itasset.service;

import com.example.itasset.domain.AssetAdjustment;
import com.example.itasset.domain.DepreciationMethod;
import com.example.itasset.domain.HardwareAsset;
import com.example.itasset.repo.AssetAdjustmentRepository;
import com.example.itasset.repo.DepreciationEntryRepository;
import com.example.itasset.repo.HardwareAssetRepository;
import com.example.itasset.support.MysqlNamedLock;
import com.example.itasset.support.Periods;
import com.example.itasset.web.ApiException;
import com.example.itasset.web.dto.AdjustmentRequest;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.util.List;

/**
 * Accounting adjustments (cost / salvage / useful life / method).
 *
 * <p>Rules:
 * <ul>
 *   <li>Only periods strictly after the closed boundary may be affected.</li>
 *   <li>The effective period must be the next unposted month ({@code lastPostedPeriod+1})
 *       for the asset: booked open periods are reversed and the new regime applies from
 *       the effective period onward — no holes in the ledger.</li>
 *   <li>Closed-period entries are never touched; current NBV is the new regime's base.</li>
 *   <li>Old parameters, new parameters and the reason are stored in an append-only row.</li>
 * </ul>
 */
@Service
public class AdjustmentService {

    private static final String MONTH_END_LOCK = "itasset_month_end";

    private final HardwareAssetRepository assetRepository;
    private final AssetAdjustmentRepository adjustmentRepository;
    private final DepreciationEntryRepository entryRepository;
    private final PeriodService periodService;
    private final MysqlNamedLock namedLock;

    public AdjustmentService(HardwareAssetRepository assetRepository,
                             AssetAdjustmentRepository adjustmentRepository,
                             DepreciationEntryRepository entryRepository,
                             PeriodService periodService,
                             MysqlNamedLock namedLock) {
        this.assetRepository = assetRepository;
        this.adjustmentRepository = adjustmentRepository;
        this.entryRepository = entryRepository;
        this.periodService = periodService;
        this.namedLock = namedLock;
    }

    @Transactional(readOnly = true)
    public List<AssetAdjustment> history(Long assetId) {
        assetRepository.findById(assetId)
                .orElseThrow(() -> ApiException.notFound("Asset not found: " + assetId));
        return adjustmentRepository.findByAssetIdOrderByIdDesc(assetId);
    }

    @Transactional
    public AssetAdjustment adjust(Long assetId, String requestId, AdjustmentRequest req) {
        var existing = adjustmentRepository.findByRequestId(requestId).orElse(null);
        if (existing != null) {
            if (!existing.getAssetId().equals(assetId)) {
                throw ApiException.conflict("X-Request-Id belongs to a different asset");
            }
            return existing;
        }
        // Serialize with depreciation runs and period close.
        return namedLock.withLock(MONTH_END_LOCK, 15, () -> doAdjust(assetId, requestId, req));
    }

    private AssetAdjustment doAdjust(Long assetId, String requestId, AdjustmentRequest req) {
        HardwareAsset asset = assetRepository.lockById(assetId)
                .orElseThrow(() -> ApiException.notFound("Asset not found: " + assetId));

        String effective = req.effectivePeriod();
        Periods.parse(effective);
        periodService.assertOpen(effective);

        if (req.newSalvageValue().compareTo(req.newCost()) > 0) {
            throw ApiException.unprocessable("New salvage value cannot exceed new cost");
        }
        if (req.newUsefulLifeMonths() == null || req.newUsefulLifeMonths() <= 0) {
            throw ApiException.unprocessable("New useful life must be a positive number of months");
        }
        if (asset.getInServiceDate() == null) {
            throw ApiException.unprocessable("Asset has never been activated; activate it first");
        }

        String firstEligible = Periods.firstDepreciationPeriod(asset.getInServiceDate());
        if (effective.compareTo(firstEligible) < 0) {
            throw ApiException.unprocessable(
                    "Effective period cannot precede the first depreciation period " + firstEligible);
        }
        // No hole: cannot point past the next unposted month (post the gap months first).
        String nextUnposted = asset.getLastPostedPeriod() == null
                ? firstEligible
                : Periods.plusMonths(asset.getLastPostedPeriod(), 1);
        if (effective.compareTo(nextUnposted) > 0) {
            throw ApiException.unprocessable(
                    "Effective period cannot be later than the next unposted month " + nextUnposted);
        }

        // Base book value is the closing NBV of the last entry that survives (periods
        // before effective). Read it before reversing anything so validation runs first.
        var retained = entryRepository.lockOpenEntries(assetId, firstEligible);
        // Entries strictly before effective survive; derive base from those.
        var surviving = retained.stream().filter(e -> e.getPeriod().compareTo(effective) < 0).toList();
        BigDecimal baseNbv;
        if (surviving.isEmpty()) {
            baseNbv = asset.getCost();
        } else {
            baseNbv = surviving.get(surviving.size() - 1).getClosingNbv();
        }

        if (req.newSalvageValue().compareTo(baseNbv) > 0) {
            throw ApiException.unprocessable(
                    "New salvage value " + req.newSalvageValue()
                            + " exceeds the base book value " + baseNbv + "; NBV cannot be raised");
        }
        // Cost cannot be revised below the book value the new regime starts from.
        if (req.newCost().compareTo(baseNbv) < 0) {
            throw ApiException.unprocessable(
                    "New cost " + req.newCost() + " is below the base book value " + baseNbv);
        }

        // Validation passed: reverse booked open-period entries at/after effective. Closed
        // periods are unreachable because the effective period was asserted open above.
        int deleted = entryRepository.deleteByAssetIdAndPeriodGreaterThanEqual(assetId, effective);
        entryRepository.flush();

        AssetAdjustment audit = new AssetAdjustment();
        audit.setAssetId(assetId);
        audit.setRequestId(requestId);
        audit.setEffectivePeriod(effective);
        audit.setReason(req.reason());
        audit.setOldCost(asset.getCost());
        audit.setOldSalvageValue(asset.getSalvageValue());
        audit.setOldUsefulLifeMonths(asset.getUsefulLifeMonths());
        audit.setOldMethod(asset.getMethod().name());
        audit.setOldSlMonthly(asset.getSlMonthly());
        audit.setOldDdbRate(asset.getDdbRate());
        audit.setOpenEntriesDeleted(deleted);

        // Apply the new regime prospectively from the base book value.
        asset.setCost(req.newCost());
        asset.setSalvageValue(req.newSalvageValue());
        asset.setUsefulLifeMonths(req.newUsefulLifeMonths());
        DepreciationMethod method = DepreciationMethod.valueOf(req.newMethod());
        asset.setMethod(method);
        asset.setNbv(baseNbv);
        asset.setUsedMonths(0);
        asset.setLastPostedPeriod(surviving.isEmpty() ? null
                : surviving.get(surviving.size() - 1).getPeriod());
        if (method == DepreciationMethod.SL) {
            asset.setSlMonthly(DepreciationPolicy.straightLineMonthly(
                    baseNbv, req.newSalvageValue(), req.newUsefulLifeMonths()));
            asset.setDdbRate(null);
        } else {
            asset.setDdbRate(DepreciationPolicy.ddbMonthlyRate(req.newUsefulLifeMonths()));
            asset.setSlMonthly(null);
        }

        audit.setNewCost(req.newCost());
        audit.setNewSalvageValue(req.newSalvageValue());
        audit.setNewUsefulLifeMonths(req.newUsefulLifeMonths());
        audit.setNewMethod(method.name());
        audit.setNewSlMonthly(asset.getSlMonthly());
        audit.setNewDdbRate(asset.getDdbRate());
        audit.setCreatedAt(java.time.LocalDateTime.now());
        adjustmentRepository.save(audit);
        return audit;
    }
}
