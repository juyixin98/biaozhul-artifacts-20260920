package com.itasset.repo;

import com.itasset.domain.Asset;
import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Optional;

public interface AssetRepository extends JpaRepository<Asset, Long> {

    Optional<Asset> findByAssetCode(String assetCode);

    boolean existsByAssetCode(String assetCode);

    /**
     * 行级写锁（MySQL: SELECT ... FOR UPDATE）。
     * 状态转换、折旧计提、参数调整均先持锁，使同资产上的
     * "退役/处置与计提"事务严格串行。
     */
    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select a from Asset a where a.id = :id")
    Optional<Asset> findByIdForUpdate(@Param("id") Long id);
}
