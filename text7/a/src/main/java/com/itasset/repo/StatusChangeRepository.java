package com.itasset.repo;

import com.itasset.domain.AssetStatus;
import com.itasset.domain.StatusChange;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface StatusChangeRepository extends JpaRepository<StatusChange, Long> {

    /** 按幂等请求 ID 查找首次转换记录。 */
    Optional<StatusChange> findByRequestId(String requestId);

    List<StatusChange> findByAssetIdOrderByIdAsc(Long assetId);

    /** 最早一次进入指定状态集合的变更（用于确定退役/处置生效月）。 */
    Optional<StatusChange> findFirstByAssetIdAndToStatusInOrderByIdAsc(
            Long assetId, List<AssetStatus> statuses);
}
