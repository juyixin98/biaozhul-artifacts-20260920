<?php

declare(strict_types=1);

namespace TravelOps\Services;

use TravelOps\ApiException;
use TravelOps\Canonical;
use TravelOps\Clock;
use TravelOps\Models\AllocationRuleVersion;
use TravelOps\Models\Contract;

/**
 * Versioned cost-allocation rules. Creating a new version supersedes the
 * previous one; a settlement permanently records WHICH rule version it used,
 * so later rule changes never rewrite historical allocations.
 */
final class RuleService
{
    /**
     * @param array<int,array<string,mixed>> $rules
     */
    public function createVersion(int $contractId, array $rules, string $actor): AllocationRuleVersion
    {
        $contract = Contract::query()->find($contractId);
        if (!$contract) {
            throw ApiException::notFound('contract');
        }
        $validated = $this->validateRules($rules);

        return \TravelOps\Locks::withTransactionalLock('rules', (string) $contractId, function () use ($contract, $validated, $actor): AllocationRuleVersion {
            /** @var Contract $contract */
            $contract = Contract::query()->lockForUpdate()->find($contract->id);

            $prev = AllocationRuleVersion::query()
                ->where('contract_id', $contract->id)
                ->where('status', AllocationRuleVersion::STATUS_ACTIVE)
                ->first();
            $nextNo = ((int) AllocationRuleVersion::query()
                ->where('contract_id', $contract->id)->max('rule_version')) + 1;

            $nowStr = Clock::now()->format('Y-m-d H:i:s.u');
            $version = new AllocationRuleVersion();
            $version->fill([
                'contract_id' => $contract->id,
                'rule_version' => $nextNo,
                'rules_json' => Canonical::json($validated),
                'status' => AllocationRuleVersion::STATUS_ACTIVE,
                'created_by' => $actor,
                'created_at' => $nowStr,
                'updated_at' => $nowStr,
            ]);
            $version->save();

            if ($prev) {
                $prev->fill(['status' => AllocationRuleVersion::STATUS_SUPERSEDED, 'updated_at' => $nowStr]);
                $prev->save();
            }
            return $version;
        });
    }

    public function activeVersion(int $contractId): ?AllocationRuleVersion
    {
        return AllocationRuleVersion::query()
            ->where('contract_id', $contractId)
            ->where('status', AllocationRuleVersion::STATUS_ACTIVE)
            ->first();
    }

    /**
     * @param array<int,array<string,mixed>> $rules
     * @return list<array{target:string,weight:int,remainder_owner:bool}>
     */
    public function validateRules(array $rules): array
    {
        if (!array_is_list($rules) || count($rules) < 1) {
            throw new ApiException('rules must be a non-empty array', 422);
        }
        $out = [];
        $targets = [];
        $owners = 0;
        foreach ($rules as $i => $rule) {
            $target = trim((string) ($rule['target'] ?? ''));
            $weight = $rule['weight'] ?? null;
            if ($target === '') {
                throw new ApiException("rules[{$i}].target is required", 422);
            }
            if (!is_int($weight) || $weight <= 0) {
                throw new ApiException("rules[{$i}].weight must be a positive integer", 422);
            }
            if (isset($targets[$target])) {
                throw new ApiException("duplicate allocation target: {$target}", 422);
            }
            $targets[$target] = true;
            $isOwner = !empty($rule['remainder_owner']);
            if ($isOwner) {
                $owners++;
            }
            $out[] = ['target' => $target, 'weight' => $weight, 'remainder_owner' => $isOwner];
        }
        if ($owners > 1) {
            throw new ApiException('at most one rule target may be the rounding remainder owner', 422);
        }
        return $out;
    }
}
