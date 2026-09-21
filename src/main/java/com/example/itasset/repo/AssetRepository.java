package com.example.itasset.repo;

import com.example.itasset.domain.Asset;
import jakarta.persistence.LockModeType;
import jakarta.persistence.QueryHint;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.jpa.repository.QueryHints;
import org.springframework.data.repository.query.Param;

import java.util.Optional;

public interface AssetRepository extends JpaRepository<Asset, Long> {

    Optional<Asset> findByAssetCode(String assetCode);

    /**
     * 悲观行锁：折旧计提与处置/退役并发时，将对同一资产的操作串行化，
     * 保证“状态变化 + 分录写入”不会产生不一致账目。
     */
    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @QueryHints(@QueryHint(name = "jakarta.persistence.lock.timeout", value = "5000"))
    @Query("select a from Asset a where a.id = :id")
    Optional<Asset> lockById(@Param("id") Long id);
}
