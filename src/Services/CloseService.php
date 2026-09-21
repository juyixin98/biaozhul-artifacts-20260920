<?php

declare(strict_types=1);

namespace Meridian\Services;

use Meridian\DomainException;
use Meridian\Models\Adjustment;
use Meridian\Models\DailyClose;
use Meridian\Models\Settlement;
use Meridian\Models\SettlementLine;
use Meridian\Support\LockManager;
use Meridian\Support\MysqlLockManager;
use Meridian\Support\Money;
use Meridian\Support\NullLockManager;

/**
 * Daily close and post-close corrections.
 *
 *  - close(date) is idempotent and atomic: the per-date named lock plus the
 *    unique close_date row guarantee exactly one close record, and imports /
 *    settlements take the same lock, so a day is either fully open or fully
 *    closed — never half-closed, never missing lines.
 *  - After close, records for that date are immutable. Corrections are posted
 *    as reversal / adjustment entries against a later, still-open business
 *    date; the original rows are never modified.
 */
final class CloseService
{
    private LockManager $locks;

    public function __construct(?LockManager $locks = null)
    {
        $this->locks = $locks ?? ((getenv('DB_DRIVER') ?: 'sqlite') === 'mysql'
            ? new MysqlLockManager()
            : new NullLockManager());
    }

    public function close(string $date, string $operator): DailyClose
    {
        if (!preg_match('/^\d{4}-\d{2}-\d{2}$/', $date) || !strtotime($date)) {
            throw new DomainException("invalid close date '{$date}'", 422, 'invalid_date');
        }

        $this->locks->acquire(ImportService::closeLockName($date));
        try {
            $existing = DailyClose::where('close_date', $date)->first();
            if ($existing !== null) {
                return $existing; // idempotent retry
            }
            return DailyClose::create(['close_date' => $date, 'closed_by' => $operator]);
        } finally {
            $this->locks->release(ImportService::closeLockName($date));
        }
    }

    /**
     * Post a correction for a record whose business date is already closed.
     *
     * @param 'line'|'settlement' $targetType
     * @param 'reversal'|'adjustment' $type
     * @param string|null $amount decimal string, required for type=adjustment
     */
    public function adjust(
        string $targetType,
        int $targetId,
        string $type,
        ?string $amount,
        string $businessDate,
        string $reason,
        string $operator,
    ): Adjustment {
        if (!in_array($type, ['reversal', 'adjustment'], true)) {
            throw new DomainException("unknown adjustment type '{$type}'", 422, 'invalid_type');
        }

        [$originalDate, $originalAmountMinor, $currency] = match ($targetType) {
            'line' => $this->lineFacts($targetId),
            'settlement' => $this->settlementFacts($targetId),
            default => throw new DomainException("unknown target_type '{$targetType}'", 422, 'invalid_target'),
        };

        if (!preg_match('/^\d{4}-\d{2}-\d{2}$/', $businessDate) || !strtotime($businessDate)) {
            throw new DomainException("invalid business_date '{$businessDate}'", 422, 'invalid_date');
        }
        if ($businessDate <= $originalDate) {
            throw new DomainException(
                "corrections must be posted to a later open day (original date {$originalDate})",
                422,
                'invalid_date'
            );
        }

        $amountMinor = match ($type) {
            'reversal' => -$originalAmountMinor,
            'adjustment' => $amount !== null
                ? Money::toMinor($amount, $currency)
                : throw new DomainException('amount is required for adjustments', 422, 'invalid_amount'),
        };

        $this->locks->acquire(ImportService::closeLockName($businessDate));
        try {
            if (DailyClose::where('close_date', $businessDate)->exists()) {
                throw new DomainException("business date {$businessDate} is closed", 409, 'date_closed');
            }

            return Adjustment::create([
                'target_type' => $targetType,
                'target_id' => $targetId,
                'type' => $type,
                'amount_minor' => $amountMinor,
                'business_date' => $businessDate,
                'reason' => $reason,
                'operator' => $operator,
            ]);
        } finally {
            $this->locks->release(ImportService::closeLockName($businessDate));
        }
    }

    /** @return array{0:string,1:int,2:string} [business_date, amount_minor, currency] */
    private function lineFacts(int $id): array
    {
        $line = SettlementLine::find($id)
            ?? throw new DomainException("settlement line {$id} not found", 404, 'line_not_found');
        return [(string) $line->business_date, (int) $line->amount_minor, $line->currency];
    }

    /** @return array{0:string,1:int,2:string} */
    private function settlementFacts(int $id): array
    {
        $settlement = Settlement::find($id)
            ?? throw new DomainException("settlement {$id} not found", 404, 'settlement_not_found');
        return [(string) $settlement->business_date, (int) $settlement->total_minor, $settlement->currency];
    }
}
