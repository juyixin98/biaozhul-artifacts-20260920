<?php

declare(strict_types=1);

namespace Meridian\Services;

use Illuminate\Database\Capsule\Manager as DB;
use Illuminate\Database\QueryException;
use Meridian\DomainException;
use Meridian\Models\Contract;
use Meridian\Models\DailyClose;
use Meridian\Models\ImportBatch;
use Meridian\Models\SettlementLine;
use Meridian\Support\LockManager;
use Meridian\Support\Money;
use Meridian\Support\MysqlLockManager;
use Meridian\Support\NullLockManager;

/**
 * Supplier settlement-line imports.
 *
 *  - Amounts are fixed-point minor units (Money::toMinor), never floats.
 *  - Lines are keyed by external_ref (外部流水ID). Re-importing the identical
 *    line is a dedup no-op; the same ref with different content is a conflict.
 *  - Any validation failure or conflict rolls back the whole batch — the
 *    batch row itself only exists for fully committed batches.
 *  - Business dates already closed by daily close are rejected. The per-date
 *    named lock serialises import against CloseService::close() so a close
 *    and an import can never interleave (no missed lines, no half-close).
 */
final class ImportService
{
    private LockManager $locks;

    public function __construct(?LockManager $locks = null)
    {
        $this->locks = $locks ?? ((getenv('DB_DRIVER') ?: 'sqlite') === 'mysql'
            ? new MysqlLockManager()
            : new NullLockManager());
    }

    /**
     * @param array<int,array<string,mixed>> $lines
     */
    public function import(string $source, array $lines, string $operator): ImportBatch
    {
        if ($lines === []) {
            throw new DomainException('import batch must contain at least one line', 422, 'empty_batch');
        }

        // ---- Phase 1: validate everything before writing anything. ----
        $prepared = [];
        foreach ($lines as $i => $line) {
            $prepared[] = $this->validateLine($line, $i);
        }

        $dates = array_values(array_unique(array_column($prepared, 'business_date')));
        sort($dates); // deterministic lock order

        // ---- Phase 2: lock the touched business dates, then commit atomically. ----
        foreach ($dates as $date) {
            $this->locks->acquire(self::closeLockName($date));
        }
        try {
            foreach ($dates as $date) {
                if (DailyClose::where('close_date', $date)->exists()) {
                    throw new DomainException("business date {$date} is closed", 409, 'date_closed');
                }
            }

            return DB::transaction(function () use ($source, $operator, $prepared) {
                $batch = ImportBatch::create([
                    'batch_no' => sprintf('%s-%s', $source, bin2hex(random_bytes(8))),
                    'source' => $source,
                    'status' => 'committed',
                    'created_by' => $operator,
                ]);

                $inserted = 0;
                $duplicates = 0;
                foreach ($prepared as $row) {
                    try {
                        SettlementLine::create($row + ['batch_id' => $batch->id, 'status' => 'active']);
                        $inserted++;
                    } catch (QueryException $e) {
                        if (!$this->isDuplicateKey($e)) {
                            throw $e;
                        }
                        $existing = SettlementLine::where('external_ref', $row['external_ref'])->first();
                        if ($existing !== null && $existing->content_hash === $row['content_hash']) {
                            $duplicates++; // identical re-import: dedup
                            continue;
                        }
                        throw new DomainException(
                            "external_ref '{$row['external_ref']}' already exists with different content",
                            409,
                            'external_ref_conflict'
                        );
                    }
                }

                $batch->inserted_count = $inserted;
                $batch->duplicate_count = $duplicates;
                $batch->save();

                return $batch->fresh(['lines']);
            });
        } finally {
            foreach ($dates as $date) {
                $this->locks->release(self::closeLockName($date));
            }
        }
    }

    public static function closeLockName(string $date): string
    {
        return 'meridian:close:' . $date;
    }

    /**
     * @param array<string,mixed> $line
     * @return array<string,mixed> row ready for settlement_lines insert
     */
    private function validateLine(array $line, int $index): array
    {
        $where = "line {$index}";

        $ref = $line['external_ref'] ?? null;
        if (!is_string($ref) || trim($ref) === '') {
            throw new DomainException("{$where}: external_ref is required", 422, 'invalid_line');
        }

        $code = $line['contract_code'] ?? null;
        $contract = is_string($code) ? Contract::where('code', $code)->first() : null;
        if ($contract === null) {
            throw new DomainException("{$where}: unknown contract_code '{$code}'", 422, 'invalid_line');
        }

        $currency = strtoupper((string) ($line['currency'] ?? ''));
        if (!Money::isKnownCurrency($currency)) {
            throw new DomainException("{$where}: invalid currency '{$currency}'", 422, 'invalid_line');
        }

        $date = (string) ($line['business_date'] ?? '');
        if (!preg_match('/^\d{4}-\d{2}-\d{2}$/', $date) || !strtotime($date)) {
            throw new DomainException("{$where}: invalid business_date '{$date}'", 422, 'invalid_line');
        }

        $amount = $line['amount'] ?? null;
        if (!is_string($amount) && !is_int($amount)) {
            throw new DomainException("{$where}: amount must be a decimal string", 422, 'invalid_line');
        }
        try {
            $amountMinor = Money::toMinor((string) $amount, $currency);
        } catch (DomainException $e) {
            throw new DomainException("{$where}: " . $e->getMessage(), 422, 'invalid_line');
        }

        $description = isset($line['description']) ? (string) $line['description'] : null;

        return [
            'contract_id' => $contract->id,
            'currency' => $currency,
            'business_date' => $date,
            'external_ref' => $ref,
            'amount_minor' => $amountMinor,
            'description' => $description,
            'content_hash' => hash('sha256', implode('|', [
                $contract->id, $currency, $date, $amountMinor, $ref, $description ?? '',
            ])),
        ];
    }

    private function isDuplicateKey(QueryException $e): bool
    {
        if (($e->errorInfo[1] ?? null) === 1062) { // MySQL duplicate entry
            return true;
        }
        return str_contains(strtolower($e->getMessage()), 'unique'); // sqlite
    }
}
