<?php

declare(strict_types=1);

namespace Meridian\Services;

use Illuminate\Database\Capsule\Manager as DB;
use Illuminate\Database\QueryException;
use Meridian\DomainException;
use Meridian\Models\AllocationRuleVersion;
use Meridian\Models\Contract;
use Meridian\Models\ContractVersion;
use Meridian\Models\DailyClose;
use Meridian\Models\Settlement;
use Meridian\Models\SettlementAllocation;
use Meridian\Models\SettlementLine;
use Meridian\Support\LockManager;
use Meridian\Support\MysqlLockManager;
use Meridian\Support\NullLockManager;

/**
 * Settlement generation: aggregate active settlement lines for
 * (contract, currency, business date) and split the total by a versioned
 * allocation rule. Every settlement is bound to a fully signed contract
 * version — never to a draft, superseded or still-signing one.
 */
final class SettlementService
{
    private LockManager $locks;

    public function __construct(
        private readonly AllocationService $allocator = new AllocationService(),
        ?LockManager $locks = null,
    ) {
        $this->locks = $locks ?? ((getenv('DB_DRIVER') ?: 'sqlite') === 'mysql'
            ? new MysqlLockManager()
            : new NullLockManager());
    }

    /**
     * Create a new version of a named allocation rule. Rules are immutable
     * once created; settlements reference an exact rule version.
     *
     * @param array<int,array{target:string,weight:int}> $allocations
     */
    public function createRuleVersion(string $name, array $allocations, ?string $remainderTarget, string $operator): AllocationRuleVersion
    {
        // Validate eagerly by running a zero allocation through the allocator.
        (new AllocationService())->allocate(0, $allocations, $remainderTarget);

        return DB::transaction(function () use ($name, $allocations, $remainderTarget, $operator) {
            $nextNo = (int) AllocationRuleVersion::where('name', $name)->max('version_no') + 1;
            return AllocationRuleVersion::create([
                'name' => $name,
                'version_no' => $nextNo,
                'rules' => array_values($allocations),
                'remainder_target' => $remainderTarget,
                'created_by' => $operator,
            ]);
        });
    }

    public function generate(
        int $contractId,
        ?int $contractVersionId,
        string $currency,
        string $businessDate,
        int $ruleVersionId,
        string $operator,
    ): Settlement {
        $contract = Contract::find($contractId)
            ?? throw new DomainException("contract {$contractId} not found", 404, 'contract_not_found');

        $version = $contractVersionId !== null
            ? ContractVersion::where('contract_id', $contractId)->find($contractVersionId)
            : ContractVersion::where('contract_id', $contractId)->where('status', 'signed')
                ->orderByDesc('version_no')->first();
        if ($version === null || $version->status !== 'signed') {
            throw new DomainException(
                'settlement requires a fully signed contract version',
                409,
                'contract_not_signed'
            );
        }

        $rule = AllocationRuleVersion::find($ruleVersionId)
            ?? throw new DomainException("allocation rule version {$ruleVersionId} not found", 404, 'rule_not_found');

        if (!preg_match('/^\d{4}-\d{2}-\d{2}$/', $businessDate)) {
            throw new DomainException('invalid business_date', 422, 'invalid_date');
        }

        $this->locks->acquire(ImportService::closeLockName($businessDate));
        try {
            if (DailyClose::where('close_date', $businessDate)->exists()) {
                throw new DomainException("business date {$businessDate} is closed", 409, 'date_closed');
            }

            $lines = SettlementLine::where('contract_id', $contract->id)
                ->where('currency', strtoupper($currency))
                ->where('business_date', $businessDate)
                ->where('status', 'active')
                ->get();
            if ($lines->isEmpty()) {
                throw new DomainException('no active settlement lines for this contract/currency/date', 422, 'no_lines');
            }
            $total = (int) $lines->sum('amount_minor');

            $shares = $this->allocator->allocate($total, $rule->rules, $rule->remainder_target);

            return DB::transaction(function () use ($contract, $version, $currency, $businessDate, $rule, $total, $shares, $operator) {
                try {
                    $settlement = Settlement::create([
                        'contract_id' => $contract->id,
                        'contract_version_id' => $version->id,
                        'currency' => strtoupper($currency),
                        'business_date' => $businessDate,
                        'rule_version_id' => $rule->id,
                        'total_minor' => $total,
                        'status' => 'final',
                        'created_by' => $operator,
                    ]);
                } catch (QueryException $e) {
                    if (($e->errorInfo[1] ?? null) === 1062
                        || str_contains(strtolower($e->getMessage()), 'unique')) {
                        throw new DomainException(
                            'settlement already exists for this version/currency/date/rule',
                            409,
                            'settlement_exists'
                        );
                    }
                    throw $e;
                }

                $allocated = 0;
                foreach ($shares as $share) {
                    SettlementAllocation::create([
                        'settlement_id' => $settlement->id,
                        'target' => $share['target'],
                        'weight' => $share['weight'],
                        'amount_minor' => $share['amount_minor'],
                        'is_remainder_sink' => $share['is_remainder_sink'],
                    ]);
                    $allocated += $share['amount_minor'];
                }
                if ($allocated !== $total) {
                    throw new \LogicException('allocation sum does not match settlement total');
                }

                return $settlement->fresh(['allocations']);
            });
        } finally {
            $this->locks->release(ImportService::closeLockName($businessDate));
        }
    }

    public function getSettlement(int $id): Settlement
    {
        return Settlement::with('allocations')->find($id)
            ?? throw new DomainException("settlement {$id} not found", 404, 'settlement_not_found');
    }
}
