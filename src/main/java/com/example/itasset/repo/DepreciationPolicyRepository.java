package com.example.itasset.repo;

import com.example.itasset.domain.DepreciationPolicy;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface DepreciationPolicyRepository extends JpaRepository<DepreciationPolicy, Long> {

    List<DepreciationPolicy> findByAssetIdOrderBySequenceNoAsc(Long assetId);

    Optional<DepreciationPolicy> findFirstByAssetIdOrderBySequenceNoDesc(Long assetId);
}
