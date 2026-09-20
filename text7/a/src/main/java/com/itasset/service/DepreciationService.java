package com.itasset.service;

import com.itasset.domain.Asset;
import com.itasset.domain.AssetStatus;
import com.itasset.domain.DepreciationEntry;
import com.itasset.domain.DepreciationMethod;
import com.itasset.domain.DepreciationRun;
import com.itasset.domain.ParameterAdjustment;
import com.itasset.domain.StatusChange;
import com.itasset.repo.AssetRepository;
import com.itasset.repo.DepreciationEntryRepository;
import com.itasset.repo.DepreciationRunRepository;
import com.itasset.repo.ParameterAdjustmentRepository;
import com.itasset.repo.PeriodCloseRepository;
import com.itasset.repo.PeriodMutexRepository;
import com.itasset.repo.StatusChangeRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * 月度折旧计提。
 *
 * <h3>关键规则</h3>
 * <ul>
 *   <li>启用次月起计提；新期间必须连续，不允许跳期补提；</li>
 *   <li>任务按 (资产, 期间) 唯一：已有分录的期间自动跳过，重跑绝不重复记账；
 *       requestId 唯一保证同一请求重放返回首次结果；</li>
 *   <li>已关账期间不可计提/重算：每个新期间先对 period_close 对应行加锁
 *       （无行则持有 InnoDB 间隙锁），与关账事务互斥；</li>
 *   <li>退役/处置当月仍可计提，次月起拒绝计提；与状态转换共用资产行锁，
 *       "退役/处置与计提并发"严格串行，账目不可能不一致；</li>
 *   <li>参数调整采用未来适用法，开启新折旧段（见 {@link ParameterAdjustmentService}）。</li>
 * </ul>
 */
@Service
public class DepreciationService {

    private static final DateTimeFormatter PERIOD_FMT = DateTimeFormatter.ofPattern("yyyyMM");

    private final AssetRepository assets;
    private final DepreciationEntryRepository entries;
    private final DepreciationRunRepository runs;
    private final ParameterAdjustmentRepository adjustments;
    private final PeriodCloseRepository closes;
    private final PeriodMutexRepository mutexes;
    private final StatusChangeRepository changes;

    public DepreciationService(AssetRepository assets, DepreciationEntryRepository entries,
                               DepreciationRunRepository runs,
                               ParameterAdjustmentRepository adjustments,
                               PeriodCloseRepository closes,
                               PeriodMutexRepository mutexes, StatusChangeRepository changes) {
        this.assets = assets;
        this.entries = entries;
        this.runs = runs;
        this.adjustments = adjustments;
        this.closes = closes;
        this.mutexes = mutexes;
        this.changes = changes;
    }

    public record RunResult(String requestId, Long assetId, String fromPeriod, String toPeriod,
                            List<DepreciationEntry> createdEntries, List<String> skippedPeriods) {
    }

    @Transactional
    public RunResult runDepreciation(Long assetId, String fromPeriod, String toPeriod,
                                     String requestId) {
        validatePeriod(fromPeriod);
        validatePeriod(toPeriod);
        if (fromPeriod.compareTo(toPeriod) > 0) {
            throw new BusinessRuleException("fromPeriod 不能晚于 toPeriod");
        }
        if (requestId == null || requestId.isBlank()) {
            throw new BusinessRuleException("requestId 不能为空");
        }

        // 资产行锁：与转换/调整事务串行。
        Asset asset = assets.findByIdForUpdate(assetId)
                .orElseThrow(() -> new NotFoundException("资产不存在: " + assetId));

        // requestId 幂等：重放返回首次运行结果，不再记账。
        DepreciationRun prior = runs.findByRequestId(requestId).orElse(null);
        if (prior != null) {
            if (!prior.getAssetId().equals(assetId)
                    || !prior.getFromPeriod().equals(fromPeriod)
                    || !prior.getToPeriod().equals(toPeriod)) {
                throw new ConflictException("requestId 已用于其他折旧任务");
            }
            return replay(assetId, fromPeriod, toPeriod, requestId);
        }

        String firstPeriod = Periods.firstDepreciationPeriod(asset.getInServiceDate());
        String stopPeriod = resolveStopPeriod(assetId);

        List<DepreciationEntry> existing = entries.findByAssetIdOrderByPeriodAsc(assetId);
        Map<String, DepreciationEntry> byPeriod = new HashMap<>();
        existing.forEach(e -> byPeriod.put(e.getPeriod(), e));
        String maxPosted = existing.isEmpty() ? null
                : existing.get(existing.size() - 1).getPeriod();
        String expectedNext = maxPosted == null ? firstPeriod : Periods.plus(maxPosted, 1);

        List<ParameterAdjustment> adjList = adjustments.findByAssetIdOrderByIdAsc(assetId);
        String finalPeriod = resolveFinalPeriod(firstPeriod, asset, adjList);

        List<DepreciationEntry> created = new ArrayList<>();
        List<String> skipped = new ArrayList<>();

        for (String p = fromPeriod; p.compareTo(toPeriod) <= 0; p = Periods.plus(p, 1)) {
            if (byPeriod.containsKey(p)) {
                // 已计提：原样跳过（重跑不重算；已关账期间的分录也是历史事实）。
                skipped.add(p);
                continue;
            }

            // 以下均为"新期间"。
            if (p.compareTo(firstPeriod) < 0) {
                throw new BusinessRuleException("启用次月（" + firstPeriod + "）之前不可计提折旧");
            }
            String currentPeriod = java.time.YearMonth.now(java.time.ZoneOffset.UTC).format(
                    java.time.format.DateTimeFormatter.ofPattern("yyyyMM"));
            if (p.compareTo(currentPeriod) > 0) {
                throw new BusinessRuleException("不可计提未来期间 " + p + "（当前期间 " + currentPeriod + "）");
            }
            if (!p.equals(expectedNext)) {
                throw new ConflictException("折旧期间不连续：下一应计提期间为 " + expectedNext
                        + "，请求在 " + p + " 新增分录（不允许跳期/补提）");
            }
            if (finalPeriod != null && p.compareTo(finalPeriod) > 0) {
                throw new ConflictException("期间 " + p + " 已超出使用年限（最后期间 "
                        + finalPeriod + "），账面价值应已等于残值");
            }
            if (stopPeriod != null && p.compareTo(stopPeriod) > 0) {
                throw new ConflictException("资产在 " + stopPeriod
                        + " 已退役/处置，当月仍可计提，期间 " + p + " 不可再计提");
            }

            // 期间互斥量：与关账/调整事务在该期间上严格串行（asset 锁 -> period 锁）。
            final String period = p;
            mutexes.insertIgnore(period);
            mutexes.lock(period)
                    .orElseThrow(() -> new IllegalStateException("period_mutex 缺失: " + period));
            if (closes.isPeriodFrozen(p)) {
                throw new ConflictException("期间 " + p + " 已关账，不可计提或重算折旧");
            }

            DepreciationEntry entry = computeEntry(asset, adjList, byPeriod, p, firstPeriod, requestId);
            entries.save(entry);
            byPeriod.put(p, entry);
            created.add(entry);
            expectedNext = Periods.plus(p, 1);
        }

        runs.save(new DepreciationRun(requestId, assetId, fromPeriod, toPeriod));
        if (!created.isEmpty()) {
            String maxCreated = created.get(created.size() - 1).getPeriod();
            if (asset.getLastDepreciatedPeriod() == null
                    || maxCreated.compareTo(asset.getLastDepreciatedPeriod()) > 0) {
                asset.setLastDepreciatedPeriod(maxCreated);
            }
        }
        return new RunResult(requestId, assetId, fromPeriod, toPeriod, created, skipped);
    }

    private RunResult replay(Long assetId, String from, String to, String requestId) {
        List<DepreciationEntry> inRange = entries.findFromPeriod(assetId, from).stream()
                .filter(e -> e.getPeriod().compareTo(to) <= 0)
                .toList();
        return new RunResult(requestId, assetId, from, to, inRange, List.of());
    }

    /** 当前参数/调整段下的最后一个可计提期间。 */
    private String resolveFinalPeriod(String firstPeriod, Asset asset,
                                      List<ParameterAdjustment> adjList) {
        if (adjList.isEmpty()) {
            return Periods.plus(firstPeriod, asset.getUsefulLifeMonths() - 1L);
        }
        ParameterAdjustment last = adjList.get(adjList.size() - 1);
        return Periods.plus(last.getEffectivePeriod(), last.getSegmentMonths() - 1L);
    }

    /** 计算单个期间的金额（折旧段模型）。 */
    private DepreciationEntry computeEntry(Asset asset, List<ParameterAdjustment> adjList,
                                           Map<String, DepreciationEntry> byPeriod,
                                           String period, String firstPeriod, String requestId) {
        ParameterAdjustment seg = null;
        for (ParameterAdjustment a : adjList) {
            if (a.getEffectivePeriod().compareTo(period) <= 0) {
                seg = a;
            }
        }

        String segStart;
        int segMonths;
        BigDecimal segBaseBook;
        BigDecimal salvage;
        DepreciationMethod method;
        BigDecimal ratePct;
        BigDecimal costSnapshot;
        int lifeSnapshot;

        if (seg == null) {
            segStart = firstPeriod;
            segMonths = asset.getUsefulLifeMonths();
            segBaseBook = asset.getPurchaseCost();
            salvage = asset.getSalvageValue();
            method = asset.getDepreciationMethod();
            ratePct = asset.getDecliningRatePct();
            costSnapshot = asset.getPurchaseCost();
            lifeSnapshot = asset.getUsefulLifeMonths();
        } else {
            segStart = seg.getEffectivePeriod();
            segMonths = seg.getSegmentMonths();
            segBaseBook = seg.getBookValueAtAdjustment();
            salvage = seg.getNewSalvageValue();
            method = seg.getNewDepreciationMethod();
            ratePct = seg.getNewDecliningRatePct();
            costSnapshot = seg.getNewPurchaseCost() != null
                    ? seg.getNewPurchaseCost() : segBaseBook;
            lifeSnapshot = seg.getNewUsefulLifeMonths();
        }

        int segIndex = (int) Periods.monthsBetween(segStart, period) + 1;
        BigDecimal opening;
        if (period.equals(segStart)) {
            opening = segBaseBook;
        } else {
            DepreciationEntry prev = byPeriod.get(Periods.plus(period, -1));
            if (prev == null) {
                throw new ConflictException("期间 " + period + " 缺少上一期间分录，不能计提");
            }
            opening = prev.getClosingBookValue();
        }

        BigDecimal base = method == DepreciationMethod.STRAIGHT_LINE
                ? segBaseBook.subtract(salvage) : null;
        DepreciationCalculator.Result r = DepreciationCalculator.calculate(
                new DepreciationCalculator.Input(method, opening, salvage, base,
                        segIndex, segMonths, ratePct));

        return new DepreciationEntry(asset.getId(), period, method, r.monthlyRatePct(),
                r.openingBookValue(), r.depreciationAmount(), r.closingBookValue(),
                costSnapshot, lifeSnapshot, segIndex, requestId);
    }

    /**
     * 退役/处置生效期间：最早一次进入 RETIRED/DISPOSED 的变更所在月。
     * 该月仍计提，次月起停提。
     */
    private String resolveStopPeriod(Long assetId) {
        return changes.findFirstByAssetIdAndToStatusInOrderByIdAsc(assetId,
                        List.of(AssetStatus.RETIRED, AssetStatus.DISPOSED))
                .map((StatusChange c) -> c.getCreatedAt().format(PERIOD_FMT))
                .orElse(null);
    }

    private void validatePeriod(String p) {
        if (p == null || !p.matches("\\d{6}")) {
            throw new BusinessRuleException("期间格式必须为 yyyyMM: " + p);
        }
        int m = Integer.parseInt(p.substring(4));
        if (m < 1 || m > 12) {
            throw new BusinessRuleException("期间月份非法: " + p);
        }
    }
}
