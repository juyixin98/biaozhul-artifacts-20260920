package com.example.itasset.repo;

import com.example.itasset.domain.IdempotentRequest;
import org.springframework.data.jpa.repository.JpaRepository;

public interface IdempotentRequestRepository extends JpaRepository<IdempotentRequest, String> {
}
