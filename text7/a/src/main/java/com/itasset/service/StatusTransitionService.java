package com.itasset.service;

import com.itasset.domain.Asset;
import com.itasset.domain.AssetStatus;
import com.itasset.domain.StatusChange;
import com.itasset.repo.AssetRepository;
import com.itasset.repo.StatusChangeRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 资产状态转换。
 *
 * <p>并发/幂等语义：
 * <ul>
 *   <li>每个请求携带 expectedVersion（乐观锁）与 requestId（幂等键）；</li>
 *   <li>先按资产行锁串行化同资产并发；锁内复查 requestId，
 *       使同 requestId 的并发/重试在锁上排齐后幂等返回首次记录；</li>
 *   <li>状态机与版本校验均在锁内进行：并发转换只有一个成功，其余得到 409；</li>
 *   <li>request_id 唯一索引兜底跨资产/绕过路径的重复；</li>
 *   <li>资产当前状态与仅追加变更记录在同一事务提交（同生共死）。</li>
 * </ul>
 */
@Service
public class StatusTransitionService {

    private final AssetRepository assets;
    private final StatusChangeRepository changes;

    public StatusTransitionService(AssetRepository assets, StatusChangeRepository changes) {
        this.assets = assets;
        this.changes = changes;
    }

    @Transactional
    public StatusChange transition(Long assetId, AssetStatus target, Long expectedVersion,
                                   String requestId, String reason, String operator) {
        // 1) 行锁串行化同资产上的一切写操作（转换/折旧/调整共用此锁）。
        Asset asset = assets.findByIdForUpdate(assetId)
                .orElseThrow(() -> new NotFoundException("资产不存在: " + assetId));

        // 2) 锁内复查幂等键：同 requestId 前序事务已提交则原样返回。
        StatusChange existing = changes.findByRequestId(requestId).orElse(null);
        if (existing != null) {
            if (!existing.getAssetId().equals(assetId) || existing.getToStatus() != target) {
                throw new ConflictException("requestId 已用于其他转换请求");
            }
            return existing;
        }

        // 3) 版本与状态机校验。
        AssetStatus from = asset.getStatus();
        if (expectedVersion != null && !expectedVersion.equals(asset.getVersion())) {
            throw new ConflictException("版本冲突：expectedVersion=" + expectedVersion
                    + "，当前版本=" + asset.getVersion());
        }
        if (from == target) {
            throw new ConflictException("资产已处于状态 " + target);
        }
        if (!from.canTransitionTo(target)) {
            throw new ConflictException("非法状态转换: " + from + " -> " + target
                    + "（处置为终态，不可恢复使用）");
        }

        // 4) 当前状态与变更记录同事务落库；唯一索引为最后防线。
        asset.setStatus(target);
        try {
            return changes.saveAndFlush(new StatusChange(assetId, from, target,
                    expectedVersion == null ? asset.getVersion() : expectedVersion,
                    requestId, reason, operator));
        } catch (RuntimeException dup) {
            // 仅可能是跨资产 requestId 冲突：语义错误，直接拒绝（会话随事务回滚）。
            throw new ConflictException("requestId 冲突，可能为重复提交: " + requestId);
        }
    }
}
