package com.example.asset.repository;

import com.example.asset.domain.Asset;
import com.example.asset.domain.AssetStatus;
import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Collection;
import java.util.List;
import java.util.Optional;

public interface AssetRepository extends JpaRepository<Asset, Long> {

    /** 悲观写锁：状态转换、折旧计提、参数调整共用，保证并发写只有一个成功。 */
    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select a from Asset a where a.id = :id")
    Optional<Asset> findWithLockById(@Param("id") Long id);

    /** 只取 ID：避免在加锁前把未加锁的实体读入持久化上下文，导致锁定时读到过期快照。 */
    @Query("select a.id from Asset a where a.status in :statuses order by a.id")
    List<Long> findIdsByStatusIn(@Param("statuses") Collection<AssetStatus> statuses);

    List<Asset> findByStatusInOrderById(Collection<AssetStatus> statuses);

    boolean existsByAssetCode(String assetCode);
}
