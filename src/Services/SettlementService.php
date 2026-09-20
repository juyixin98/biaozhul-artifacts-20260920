<?php

declare(strict_types=1);

namespace TravelOps\Services;

use Illuminate\Database\Capsule\Manager as Capsule;
use TravelOps\ApiException;
use TravelOps\Canonical;
use TravelOps\Clock;
use TravelOps\Money;
use TravelOps\Models\Contract;
use TravelOps\Models\ContractVersion;
use TravelOps\Models\Settlement;
use TravelOps\Models\SettlementAllocation;
use TravelOps\Models\SettlementEntry;
use TravelOps\Models\SettlementEntryLink;

/**
 * Creates cost settlements over imported entries and allocates the total by
 * the versioned allocation rules.
 *
 * Invariants:
 *  - a settlement is bound to one fully-signed contract version (its
 *    content_hash is snapshotted on the settlement row); unsigned/pending,
 *    withdrawn or expired versions cannot settle;
 *  - allocation parts are computed with the largest-remainder method and
 *    ALWAYS sum exactly to the settled amount, with each rounding unit
 *    deterministically attributed (remainder_owner first, then largest
 *    fractional remainder, then declared order);
 *  - each entry settles exactly once (UNIQUE(entry_id) on the link table);
 *  - entries on a closed date cannot be settled or changed.
 */
final class SettlementService
{
    /**
     * @param array{contract_id:int, currency:string, business_date:string, idempotency_key:?string} $filter
     * @return array<string,mixed>
     */
    public function create(array $filter, string $actor): array
    {
        $contractId = (int) ($filter['contract_id'] ?? 0);
        $currency = strtoupper((string) ($filter['currency'] ?? ''));
        $date = (string) ($filter['business_date'] ?? '');
        CloseService::assertDate($date);
        if (!preg_match('/^[A-Z]{3}$/', $currency)) {
            throw new ApiException('currency must be a 3-letter ISO code', 422);
        }

        $idem = (string) ($filter['idempotency_key']
            ?? Canonical::sha256(['settle', $contractId, $currency, $date]));

        return \TravelOps\Locks::withTransactionalLock(
            'settle',
            $idem,
            function () use ($contractId, $currency, $date, $idem, $actor): array {
                $contract = Contract::query()->find($contractId);
                if (!$contract) {
                    throw ApiException::notFound('contract');
                }

                // Idempotent replay: same key returns the original settlement.
                $existing = Settlement::query()->where('idempotency_key', $idem)->first();
                if ($existing) {
                    return ['settlement' => $this->present($existing), 'idempotent' => true];
                }

                if ((new CloseService())->isClosed($date)) {
                    throw ApiException::conflict(
                        'business date is already closed; corrections belong on the next open day',
                        'date_closed'
                    );
                }

                // The binding version must be fully signed.
                Contract::query()->lockForUpdate()->find($contract->id);
                $version = $contract->current_version_id
                    ? ContractVersion::query()->lockForUpdate()->find($contract->current_version_id)
                    : null;
                if (!$version || $version->status !== ContractVersion::STATUS_SIGNED) {
                    throw ApiException::state('contract has no fully-signed version to bind the settlement to');
                }

                $rules = (new RuleService())->activeVersion($contract->id);
                if (!$rules) {
                    throw ApiException::state('contract has no active allocation rule version');
                }

                // Only as-yet-unsettled entries matching the grouping key.
                $entries = SettlementEntry::query()
                    ->where('contract_id', $contract->id)
                    ->where('currency', $currency)
                    ->where('business_date', $date)
                    ->whereNotExists(function ($q) {
                        $q->select(Capsule::raw(1))
                            ->from('settlement_entry_links')
                            ->whereColumn('settlement_entry_links.entry_id', 'settlement_entries.id');
                    })
                    ->orderBy('id')
                    ->get();

                if ($entries->isEmpty()) {
                    throw ApiException::conflict('no unsettled entries match contract/currency/business_date', 'nothing_to_settle');
                }

                $amount = '0';
                foreach ($entries as $e) {
                    $amount = Money::add($amount, (string) $e->amount);
                }

                $allocations = Money::allocate($amount, $rules->rules());
                $allocatedSum = Money::add(...array_column($allocations, 'amount'));
                if ($allocatedSum !== $amount) {
                    // Defense in depth: the allocator must conserve the total.
                    throw new \RuntimeException('allocation conservation violated');
                }

                $nowStr = Clock::now()->format('Y-m-d H:i:s.u');
                $settlement = new Settlement();
                $settlement->fill([
                    'contract_id' => $contract->id,
                    'contract_version_id' => $version->id,
                    'rule_version_id' => $rules->id,
                    'currency' => $currency,
                    'business_date' => $date,
                    'amount' => $amount,
                    'content_hash' => $version->content_hash,
                    'idempotency_key' => $idem,
                    'created_by' => $actor,
                    'created_at' => $nowStr,
                ]);
                try {
                    $settlement->save();
                } catch (\Illuminate\Database\QueryException $e) {
                    if ($e->getCode() === '23000') {
                        throw ApiException::conflict('settlement with this idempotency key already exists', 'duplicate_settlement');
                    }
                    throw $e;
                }

                foreach ($allocations as $a) {
                    $alloc = new SettlementAllocation();
                    $alloc->fill([
                        'settlement_id' => $settlement->id,
                        'target' => $a['target'],
                        'weight' => $a['weight'],
                        'allocated_amount' => $a['amount'],
                        'is_remainder_owner' => $a['is_remainder_owner'] ? 1 : 0,
                        'position' => $a['position'],
                        'created_at' => $nowStr,
                    ]);
                    $alloc->save();
                }

                foreach ($entries as $e) {
                    $link = new SettlementEntryLink();
                    try {
                        $link->fill([
                            'settlement_id' => $settlement->id,
                            'entry_id' => $e->id,
                            'amount' => (string) $e->amount,
                        ]);
                        $link->save();
                    } catch (\Illuminate\Database\QueryException $e2) {
                        if ($e2->getCode() === '23000') {
                            // Another settlement claimed an entry concurrently.
                            throw ApiException::conflict('an entry was settled concurrently, retry', 'entry_busy');
                        }
                        throw $e2;
                    }
                }

                return ['settlement' => $this->present($settlement->fresh()), 'idempotent' => false];
            }
        );
    }

    /** @return array<string,mixed> */
    public function present(Settlement $s): array
    {
        $s->loadMissing('allocations');
        return [
            'id' => (int) $s->id,
            'contract_id' => (int) $s->contract_id,
            'contract_version_id' => (int) $s->contract_version_id,
            'rule_version_id' => (int) $s->rule_version_id,
            'currency' => $s->currency,
            'business_date' => (string) $s->business_date,
            'amount' => (string) $s->amount,
            'content_hash' => $s->content_hash,
            'allocations' => array_map(static fn (SettlementAllocation $a) => [
                'target' => $a->target,
                'weight' => (int) $a->weight,
                'allocated_amount' => (string) $a->allocated_amount,
                'remainder_owner' => (bool) $a->is_remainder_owner,
            ], $s->allocations->all()),
        ];
    }
}
