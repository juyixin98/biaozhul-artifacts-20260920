package com.example.itasset.service;

import com.example.itasset.domain.DepreciationEntry;
import com.example.itasset.domain.DepreciationRun;
import com.example.itasset.domain.HardwareAsset;
import com.example.itasset.repo.DepreciationEntryRepository;
import com.example.itasset.repo.DepreciationRunRepository;
import com.example.itasset.repo.HardwareAssetRepository;
import com.example.itasset.support.MysqlNamedLock;
import com.example.itasset.support.Periods;
import com.example.itasset.web.ApiException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.time.Clock;
import java.time.YearMonth;
import java.util.ArrayList;
import java.util.List;

/**
 * Month-end depreciation posting.
 *
 * <p>A run for period P fills every still-unposted, eligible month per asset, from
 * {@code lastPostedPeriod + 1} through P. The run holds the {@code itasset_month_end}
 * advisory lock so it is mutually exclusive with period close; per-asset row locks and
 * the unique {@code (asset_id, period)} key make it safe against retirement/disposal and
 * against concurrent runs. Rerunning a period books nothing twice.
 */
@Service
public class DepreciationService {

    private static final String MONTH_END_LOCK = "itasset_month_end";

    private final HardwareAssetRepository assetRepository;
    private final DepreciationEntryRepository entryRepository;
    private final DepreciationRunRepository runRepository;
    private final PeriodService periodService;
    private final MysqlNamedLock namedLock;
    private final Clock clock;

    public DepreciationService(HardwareAssetRepository assetRepository,
                               DepreciationEntryRepository entryRepository,
                               DepreciationRunRepository runRepository,
                               PeriodService periodService,
                               MysqlNamedLock namedLock,
                               Clock clock) {
        this.assetRepository = assetRepository;
        this.entryRepository = entryRepository;
        this.runRepository = runRepository;
        this.periodService = periodService;
        this.namedLock = namedLock;
        this.clock = clock;
    }

    @Transactional
    public DepreciationRun runPeriod(String requestId, String requestedPeriod) {
        Periods.parse(requestedPeriod);
        DepreciationRun replay = runRepository.findByRequestId(requestId).orElse(null);
        if (replay != null) {
            return replay;
        }
        // Single global serialization point for all month-end work (posting AND closing).
        return namedLock.withLock(MONTH_END_LOCK, 15, () -> doRun(requestId, requestedPeriod));
    }

    private DepreciationRun doRun(String requestId, String requestedPeriod) {
        if (periodService.isClosed(requestedPeriod)) {
            throw ApiException.conflict(
                    "Period " + requestedPeriod + " is closed; depreciation cannot be booked");
        }
        if (requestedPeriod.compareTo(YearMonth.now(clock).format(Periods.FORMAT)) > 0) {
            throw ApiException.unprocessable("Cannot post depreciation for a future period");
        }
        // A prior run row for the period may exist (first month-end or a post-adjustment
        // catch-up). It does NOT block booking: entries are keyed by (asset, period) and
        // every still-missing eligible month is filled here.

        int assetsPosted = 0;
        int entriesCreated = 0;
        BigDecimal totalCharge = BigDecimal.ZERO;
        List<String> notes = new ArrayList<>();
        List<DepreciationEntry> createdEntries = new ArrayList<>();

        // Stable order with pessimistic row locks: concurrent retirement/disposal blocks here.
        List<HardwareAsset> assets = assetRepository.lockAllForPosting();
        for (HardwareAsset asset : assets) {
            PostResult r = postAsset(asset, requestedPeriod, createdEntries);
            if (r.entries() > 0) {
                assetsPosted++;
                entriesCreated += r.entries();
                totalCharge = totalCharge.add(r.charge());
            }
            if (!r.note().isBlank()) {
                notes.add(asset.getAssetCode() + ": " + r.note());
            }
        }

        String message = "Posted " + entriesCreated + " entries across " + assetsPosted + " assets"
                + (notes.isEmpty() ? "" : "; " + String.join("; ", notes));
        if (message.length() > 1900) {
            message = message.substring(0, 1900) + "…";
        }
        DepreciationRun run = new DepreciationRun(requestId, requestedPeriod, "COMPLETED",
                assetsPosted, entriesCreated, totalCharge, message);
        runRepository.saveAndFlush(run);
        // Link the booked entries to the run now that its id exists.
        for (DepreciationEntry e : createdEntries) {
            e.linkRun(run.getId());
        }
        entryRepository.flush();
        return run;
    }

    private record PostResult(int entries, BigDecimal charge, String note) {
    }

    /** Post all missing eligible months for one asset up to {@code requestedPeriod}. */
    private PostResult postAsset(HardwareAsset asset, String requestedPeriod,
                                 List<DepreciationEntry> createdEntries) {
        if (asset.getInServiceDate() == null) {
            return new PostResult(0, BigDecimal.ZERO, "not in service");
        }
        String firstEligible = Periods.firstDepreciationPeriod(asset.getInServiceDate());
        String closedThrough = periodService.closedThrough();
        String startAfter = asset.getLastPostedPeriod();
        String firstToPost = startAfter == null
                ? firstEligible
                : Periods.plusMonths(startAfter, 1);

        if (firstToPost.compareTo(requestedPeriod) > 0) {
            return new PostResult(0, BigDecimal.ZERO, "already up to date");
        }
        if (startAfter == null && closedThrough != null
                && firstToPost.compareTo(closedThrough) <= 0) {
            // Never-booked asset whose first eligible period is already closed: cannot backfill.
            return new PostResult(0, BigDecimal.ZERO,
                    "first eligible period " + firstToPost + " is already closed; manual backfill required");
        }

        int created = 0;
        BigDecimal chargeTotal = BigDecimal.ZERO;
        YearMonth cursor = Periods.parse(firstToPost);
        YearMonth end = Periods.parse(requestedPeriod);

        while (!cursor.isAfter(end)) {
            String period = Periods.of(cursor);
            if (isEligible(asset, firstEligible, period)) {
                BigDecimal opening = asset.getNbv();
                int remaining = asset.getUsefulLifeMonths() - asset.getUsedMonths();
                BigDecimal charge = DepreciationPolicy.monthlyCharge(
                        asset.getMethod(), opening, asset.getSalvageValue(),
                        asset.getSlMonthly(), asset.getDdbRate(), remaining);
                BigDecimal closing = opening.subtract(charge);

                DepreciationEntry entry = new DepreciationEntry(
                        asset.getId(), period, asset.getMethod(), opening, charge, closing, null);
                entryRepository.save(entry);
                createdEntries.add(entry);
                asset.setNbv(closing);
                asset.setUsedMonths(asset.getUsedMonths() + 1);
                created++;
                chargeTotal = chargeTotal.add(charge);
            }
            // Ineligible months still advance the marker so no gap is left behind.
            asset.setLastPostedPeriod(period);
            cursor = cursor.plusMonths(1);
        }
        return new PostResult(created, chargeTotal, created == 0 ? "no eligible months" : "");
    }

    /**
     * An asset is eligible in period P if P falls inside its depreciation window:
     * not earlier than the month after activation, and not later than the
     * retirement/disposal exit period (当月减少，当月照提).
     */
    private boolean isEligible(HardwareAsset asset, String firstEligible, String period) {
        if (period.compareTo(firstEligible) < 0) {
            return false;
        }
        if (asset.getExitPeriod() != null && period.compareTo(asset.getExitPeriod()) > 0) {
            return false;
        }
        int remaining = asset.getUsefulLifeMonths() - asset.getUsedMonths();
        return remaining > 0;
    }
}
