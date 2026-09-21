package com.example.itasset.service;

import com.example.itasset.domain.AssetStatus;
import com.example.itasset.domain.AssetTransition;
import com.example.itasset.domain.DepreciationMethod;
import com.example.itasset.domain.HardwareAsset;
import com.example.itasset.repo.AssetTransitionRepository;
import com.example.itasset.repo.HardwareAssetRepository;
import com.example.itasset.support.Periods;
import com.example.itasset.web.ApiException;
import com.example.itasset.web.dto.CreateAssetRequest;
import com.example.itasset.web.dto.TransitionRequest;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.util.List;

/**
 * Asset master data and state transitions.
 *
 * <p>Every transition:
 * <ol>
 *   <li>locks the asset row (pessimistic) to serialize concurrent lifecycle writers,</li>
 *   <li>verifies the client's expectedVersion (optimistic concurrency),</li>
 *   <li>validates the whitelisted transition and accounting preconditions,</li>
 *   <li>updates current state and appends the change record in the SAME transaction.</li>
 * </ol>
 */
@Service
public class AssetService {

    private final HardwareAssetRepository assetRepository;
    private final AssetTransitionRepository transitionRepository;
    private final PeriodService periodService;

    public AssetService(HardwareAssetRepository assetRepository,
                        AssetTransitionRepository transitionRepository,
                        PeriodService periodService) {
        this.assetRepository = assetRepository;
        this.transitionRepository = transitionRepository;
        this.periodService = periodService;
    }

    @Transactional(readOnly = true)
    public HardwareAsset get(Long id) {
        return assetRepository.findById(id)
                .orElseThrow(() -> ApiException.notFound("Asset not found: " + id));
    }

    @Transactional(readOnly = true)
    public Page<HardwareAsset> list(String department, Pageable pageable) {
        return department == null || department.isBlank()
                ? assetRepository.findAll(pageable)
                : assetRepository.findByDepartment(department, pageable);
    }

    @Transactional(readOnly = true)
    public List<AssetTransition> transitions(Long assetId) {
        get(assetId);
        return transitionRepository.findByAssetIdOrderByIdAsc(assetId);
    }

    @Transactional
    public HardwareAsset create(CreateAssetRequest req) {
        if (assetRepository.existsByAssetCode(req.assetCode())) {
            throw ApiException.conflict("Asset code already exists: " + req.assetCode());
        }
        if (req.salvageValue().compareTo(req.cost()) > 0) {
            throw ApiException.unprocessable("Salvage value cannot exceed cost");
        }
        HardwareAsset a = new HardwareAsset();
        a.setAssetCode(req.assetCode());
        a.setName(req.name());
        a.setDepartment(req.department());
        a.setStatus(AssetStatus.IN_STOCK);
        a.setCost(req.cost());
        a.setSalvageValue(req.salvageValue());
        a.setNbv(req.cost());
        a.setMethod(DepreciationMethod.valueOf(req.method()));
        a.setUsedMonths(0);
        LocalDateTime now = LocalDateTime.now();
        a.setCreatedAt(now);
        a.setUpdatedAt(now);
        return assetRepository.save(a);
    }

    /**
     * Apply a whitelisted state transition. The current-status update and the appended
     * {@link AssetTransition} commit atomically.
     *
     * @return the created (or replayed) transition
     */
    @Transactional
    public AssetTransition transition(Long assetId, String requestId, TransitionRequest req) {
        // Replay the existing append-only record if this request already succeeded.
        AssetTransition existing = transitionRepository.findByRequestId(requestId).orElse(null);
        if (existing != null) {
            if (!existing.getAssetId().equals(assetId)) {
                throw ApiException.conflict("X-Request-Id belongs to a different asset");
            }
            return existing;
        }

        // Take the write lock on the parent row FIRST, before inserting the child
        // transition. Otherwise two concurrent transitions each hold an FK shared lock
        // on this row and deadlock on their optimistic updates. The loser of the race
        // then blocks until the winner commits and fails the version check below (409).
        HardwareAsset asset = assetRepository.lockById(assetId)
                .orElseThrow(() -> ApiException.notFound("Asset not found: " + assetId));

        if (asset.getVersion() != req.expectedVersion()) {
            throw ApiException.conflict(
                    "Version mismatch: expected " + req.expectedVersion()
                            + " but current version is " + asset.getVersion());
        }

        AssetStatus from = asset.getStatus();
        AssetStatus target = req.targetStatus();
        if (!from.canTransitionTo(target)) {
            throw ApiException.unprocessable("Transition " + from + " -> " + target + " is not allowed");
        }

        String period = resolvePeriod(req.effectiveDate(), req.effectivePeriod());
        switch (target) {
            case IN_USE -> {
                if (from == AssetStatus.IN_STOCK) {
                    activate(asset, period, req);
                } else {
                    // Returning from repair: depreciation regime is unchanged.
                    asset.setStatus(AssetStatus.IN_USE);
                }
            }
            case RETIRED -> retire(asset, period, req.reason());
            case DISPOSED -> dispose(asset, period, req.reason());
            case UNDER_REPAIR -> asset.setStatus(AssetStatus.UNDER_REPAIR);
            default -> throw ApiException.unprocessable("Unsupported target: " + target);
        }
        asset.setUpdatedAt(LocalDateTime.now());

        long expected = req.expectedVersion();
        long resulted = asset.getVersion() + 1;
        AssetTransition record = new AssetTransition(assetId, requestId, from, target, period,
                req.reason(), expected, resulted);
        transitionRepository.save(record);
        assetRepository.saveAndFlush(asset); // surfaces optimistic failure immediately
        return record;
    }

    /** IN_STOCK -> IN_USE: depreciation parameters are fixed at activation. */
    private void activate(HardwareAsset asset, String period, TransitionRequest req) {
        LocalDate effectiveDate = req.effectiveDate() != null
                ? req.effectiveDate()
                : Periods.parse(period).atDay(1);
        Integer life = req.usefulLifeMonths();
        if (life == null || life <= 0) {
            throw ApiException.unprocessable("Activation requires a positive usefulLifeMonths");
        }
        if (period.compareTo(Periods.of(effectiveDate)) != 0) {
            throw ApiException.unprocessable("effectivePeriod must match effectiveDate's month");
        }
        String firstEligible = Periods.firstDepreciationPeriod(effectiveDate);
        if (periodService.isClosed(firstEligible)) {
            throw ApiException.unprocessable(
                    "Cannot activate: first depreciation period " + firstEligible + " is already closed");
        }
        asset.setInServiceDate(effectiveDate);
        asset.setUsefulLifeMonths(life);
        asset.setUsedMonths(0);
        if (asset.getMethod() == DepreciationMethod.SL) {
            asset.setSlMonthly(DepreciationPolicy.straightLineMonthly(
                    asset.getCost(), asset.getSalvageValue(), life));
            asset.setDdbRate(null);
        } else {
            asset.setDdbRate(DepreciationPolicy.ddbMonthlyRate(life));
            asset.setSlMonthly(null);
        }
        asset.setStatus(AssetStatus.IN_USE);
    }

    private void retire(HardwareAsset asset, String period, String reason) {
        validateExitPeriod(asset, period);
        asset.setExitPeriod(period);
        asset.setStatus(AssetStatus.RETIRED);
    }

    private void dispose(HardwareAsset asset, String period, String reason) {
        periodService.assertOpen(period);
        // RETIRED assets already carry an exitPeriod from retirement; the eligibility
        // window does not change on disposal, but disposal cannot precede retirement.
        if (asset.getExitPeriod() != null && period.compareTo(asset.getExitPeriod()) < 0) {
            throw ApiException.unprocessable("Disposal period cannot precede the retirement period");
        }
        if (asset.getExitPeriod() == null) {
            // IN_STOCK -> DISPOSED: never depreciated, disposal period closes the window.
            if (asset.getInServiceDate() != null
                    && period.compareTo(Periods.of(asset.getInServiceDate())) < 0) {
                throw ApiException.unprocessable("Disposal period precedes the in-service month");
            }
            asset.setExitPeriod(period);
        }
        asset.setStatus(AssetStatus.DISPOSED);
    }

    private void validateExitPeriod(HardwareAsset asset, String period) {
        periodService.assertOpen(period);
        String lastPosted = asset.getLastPostedPeriod();
        if (lastPosted != null && period.compareTo(lastPosted) < 0) {
            throw ApiException.unprocessable(
                    "Exit period " + period + " precedes the last posted period " + lastPosted);
        }
        if (asset.getInServiceDate() != null
                && period.compareTo(Periods.of(asset.getInServiceDate())) < 0) {
            throw ApiException.unprocessable("Exit period precedes the in-service month");
        }
    }

    private String resolvePeriod(LocalDate date, String period) {
        if (date != null) {
            return Periods.of(date);
        }
        if (period != null) {
            return Periods.parse(period).format(Periods.FORMAT);
        }
        return Periods.current();
    }
}
