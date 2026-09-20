<?php

declare(strict_types=1);

namespace TravelOps\Services;

use Illuminate\Database\Capsule\Manager as Capsule;
use TravelOps\ApiException;
use TravelOps\Canonical;
use TravelOps\Clock;
use TravelOps\Money;
use TravelOps\Models\Contract;
use TravelOps\Models\ImportBatch;
use TravelOps\Models\SettlementEntry;

/**
 * Supplier settlement-detail import.
 *
 * Rules:
 *  - amounts are fixed-point decimals (at most 4 places), never floats;
 *  - external_txn_id is globally unique: a repeat id with identical content is
 *    skipped as a duplicate, a repeat id with CHANGED content is a 409 conflict;
 *  - any validation failure rolls the ENTIRE batch back (all-or-nothing);
 *  - imports are serialized against daily close per business date, so an import
 *    can never be missed by a close or land after a half-closed day.
 */
final class ImportService
{
    /**
     * @param array<int,array<string,mixed>> $rows
     * @return array{batch_id:int, imported:int, duplicates:int, totals:array<string,string>}
     */
    public function importBatch(array $rows, string $actor, ?string $batchRef = null): array
    {
        if (!array_is_list($rows) || $rows === []) {
            throw new ApiException('entries must be a non-empty array', 422);
        }

        // Validate every row BEFORE opening the transaction so a bad batch
        // leaves nothing behind.
        $validated = [];
        $seenInBatch = [];
        foreach ($rows as $i => $row) {
            $v = $this->validateRow($row, $i);
            if (isset($seenInBatch[$v['external_txn_id']])) {
                throw ApiException::conflict(
                    "external_txn_id duplicated inside the batch: {$v['external_txn_id']}",
                    'duplicate_in_batch',
                    ['index' => $i]
                );
            }
            $seenInBatch[$v['external_txn_id']] = true;
            $validated[] = $v;
        }

        // One advisory lock per distinct business date, acquired in ascending
        // date order — the same canonical order the close process uses — so
        // import vs close and multi-date imports cannot deadlock.
        $dates = array_values(array_unique(array_column($validated, 'business_date')));
        sort($dates);

        return $this->withDateLocks($dates, function () use ($validated, $actor, $batchRef): array {
            $conn = Capsule::connection();

            // Closed dates reject new imports: after close records are immutable.
            $closed = $conn->table('daily_closes')
                ->whereIn('business_date', array_column($validated, 'business_date'))
                ->pluck('business_date')->all();
            if ($closed) {
                throw ApiException::conflict(
                    'business date already closed; corrections must be made on the next open day',
                    'date_closed',
                    ['closed_dates' => array_map('strval', $closed)]
                );
            }

            $nowStr = Clock::now()->format('Y-m-d H:i:s.u');

            $batch = new ImportBatch();
            $batch->fill([
                'batch_ref' => $batchRef,
                'total_count' => 0,
                'status' => 'committed',
                'created_by' => $actor,
                'created_at' => $nowStr,
            ]);
            $batch->save();

            $imported = 0;
            $duplicates = 0;
            $totals = [];

            foreach ($validated as $v) {
                $existing = SettlementEntry::query()
                    ->where('external_txn_id', $v['external_txn_id'])
                    ->first();

                if ($existing) {
                    // Same id + same content -> idempotent duplicate (skip).
                    if ($this->sameContent($existing, $v)) {
                        $duplicates++;
                        continue;
                    }
                    // Same id + different content -> hard conflict, whole batch rolls back.
                    throw ApiException::conflict(
                        "external_txn_id {$v['external_txn_id']} already exists with different content",
                        'external_id_conflict',
                        ['external_txn_id' => $v['external_txn_id']]
                    );
                }

                $entry = new SettlementEntry();
                $entry->fill([
                    'contract_id' => $v['contract_id'],
                    'currency' => $v['currency'],
                    'business_date' => $v['business_date'],
                    'external_txn_id' => $v['external_txn_id'],
                    'amount' => $v['amount'],
                    'description' => $v['description'],
                    'source' => SettlementEntry::SOURCE_IMPORT,
                    'import_batch_id' => $batch->id,
                    'created_by' => $actor,
                    'created_at' => $nowStr,
                ]);
                $entry->save();
                $imported++;

                $totals[$v['currency']] = Money::add($totals[$v['currency']] ?? '0', $v['amount']);
            }

            $batch->fill([
                'total_count' => $imported,
                'total_amount_json' => $totals === [] ? '{}' : Canonical::json($totals),
            ]);
            $batch->save();

            return ['batch_id' => (int) $batch->id, 'imported' => $imported, 'duplicates' => $duplicates, 'totals' => $totals];
        });
    }

    /**
     * Aggregation view: totals grouped by contract, currency and business date.
     *
     * @return array<int,array<string,mixed>>
     */
    public function aggregation(?int $contractId = null): array
    {
        $q = Capsule::connection()->table('settlement_entries')
            ->select('contract_id', 'currency', 'business_date')
            ->selectRaw('COUNT(*) AS row_count, SUM(amount) AS total_amount')
            ->groupBy('contract_id', 'currency', 'business_date')
            ->orderBy('contract_id')->orderBy('business_date')->orderBy('currency');
        if ($contractId !== null) {
            $q->where('contract_id', $contractId);
        }
        return array_map(static fn ($r) => [
            'contract_id' => (int) $r->contract_id,
            'currency' => $r->currency,
            'business_date' => $r->business_date,
            'row_count' => (int) $r->row_count,
            'total_amount' => $r->total_amount,
        ], $q->get()->all());
    }

    /**
     * @param array<string,mixed> $row
     * @return array<string,mixed>
     */
    private function validateRow(array $row, int $index): array
    {
        $prefix = "entries[{$index}]";
        foreach (['contract_id', 'currency', 'business_date', 'external_txn_id', 'amount'] as $required) {
            if (!array_key_exists($required, $row) || $row[$required] === null || $row[$required] === '') {
                throw new ApiException("{$prefix}.{$required} is required", 422);
            }
        }

        $contractId = (int) $row['contract_id'];
        if (!Contract::query()->where('id', $contractId)->exists()) {
            throw new ApiException("{$prefix}.contract_id does not exist", 422);
        }

        $currency = strtoupper((string) $row['currency']);
        if (!preg_match('/^[A-Z]{3}$/', $currency)) {
            throw new ApiException("{$prefix}.currency must be a 3-letter ISO code", 422);
        }

        $date = (string) $row['business_date'];
        if (!preg_match('/^\d{4}-\d{2}-\d{2}$/', $date) || !checkdate((int) substr($date, 5, 2), (int) substr($date, 8, 2), (int) substr($date, 0, 4))) {
            throw new ApiException("{$prefix}.business_date must be YYYY-MM-DD", 422);
        }

        $txnId = (string) $row['external_txn_id'];
        if (strlen($txnId) > 128) {
            throw new ApiException("{$prefix}.external_txn_id too long (max 128)", 422);
        }

        try {
            $amount = Money::normalize($row['amount']);
        } catch (\InvalidArgumentException $e) {
            throw new ApiException("{$prefix}.amount: " . $e->getMessage(), 422);
        }
        // Supplier import rows are positive charges; corrections arrive via
        // the dedicated reversal/adjustment endpoint instead.
        if (bccomp($amount, '0', Money::SCALE) <= 0) {
            throw new ApiException("{$prefix}.amount must be positive; use the correction endpoint for reversals", 422);
        }

        $description = isset($row['description']) && $row['description'] !== null
            ? mb_substr((string) $row['description'], 0, 500) : null;

        return [
            'contract_id' => $contractId,
            'currency' => $currency,
            'business_date' => $date,
            'external_txn_id' => $txnId,
            'amount' => $amount,
            'description' => $description,
        ];
    }

    /**
     * @param array<string,mixed> $v
     */
    private function sameContent(SettlementEntry $e, array $v): bool
    {
        return (int) $e->contract_id === $v['contract_id']
            && $e->currency === $v['currency']
            && (string) $e->business_date === $v['business_date']
            && Money::normalize((string) $e->amount) === $v['amount'];
    }

    /**
     * Acquire the per-date advisory locks (canonical order), then run $fn in
     * a single transaction — locks are released only after commit, so import
     * and close share the exact same serialization point.
     *
     * @template T
     * @param list<string> $dates
     * @param callable():T $fn
     * @return T
     */
    public static function withDateLocks(array $dates, callable $fn): mixed
    {
        $names = array_map(static fn (string $d): string => 'bizdate:' . $d, $dates);
        return \TravelOps\Locks::withLocks($names, $fn);
    }
}
