package com.example.itasset.repo;

import com.example.itasset.domain.AssetTransition;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface AssetTransitionRepository extends JpaRepository<AssetTransition, Long> {

    List<AssetTransition> findByAssetIdOrderByIdAsc(Long assetId);

    Optional<AssetTransition> findByRequestId(String requestId);
}
