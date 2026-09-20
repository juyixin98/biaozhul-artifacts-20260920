<?php

declare(strict_types=1);

namespace TravelOps\Services;

use TravelOps\ApiException;
use TravelOps\Clock;
use TravelOps\Money;
use TravelOps\Models\SettlementEntry;

/**
 * Corrections AFTER a date is closed.
 *
 * Closed records are immutable — nothing is edited or deleted. Instead the
 * correction is posted as new, dated entries on the next OPEN business day:
 *   - reversal: a system entry for -<original amount>, linked to the original;
 *   - adjustment: an optional signed delta (e.g. +10.0000), also linked.
 * Both new rows get their own external_txn_id, are blocked if the posting
 * date is closed, and are themselves immutable once that posting date closes.
 */
final class CorrectionService
{
    /**
     * @return array{reversal:array<string,mixed>, adjustment:?array<string,mixed>}
     */
    public function reverse(int $entryId, string $postingDate, string $actor, ?string $adjustment = null): array
    {
        CloseService::assertDate($postingDate);

        $delta = null;
        if ($adjustment !== null) {
            try {
                $delta = Money::normalize($adjustment);
            } catch (\InvalidArgumentException $e) {
                throw new ApiException('adjustment: ' . $e->getMessage(), 422);
            }
            if (bccomp($delta, '0', Money::SCALE) === 0) {
                throw new ApiException('adjustment must be non-zero', 422);
            }
        }

        // Locks in canonical date order: posting date first, then the
        // original entry's date only when it differs.
        $lockDates = [$postingDate];

        return ImportService::withDateLocks($lockDates, function () use ($entryId, $postingDate, $actor, $delta): array {
            if ((new CloseService())->isClosed($postingDate)) {
                throw ApiException::conflict('posting date is already closed; choose a later open day', 'posting_date_closed');
            }

            /** @var SettlementEntry|null $original */
            $original = SettlementEntry::query()->lockForUpdate()->find($entryId);
            if (!$original) {
                throw ApiException::notFound('original settlement entry');
            }
            if ($original->source !== SettlementEntry::SOURCE_IMPORT) {
                throw ApiException::state('only imported entries can be reversed');
            }
            if ((string) $original->business_date === $postingDate) {
                throw new ApiException(
                    'correction must be posted on a LATER day than the original entry',
                    422
                );
            }
            if ($postingDate < (string) $original->business_date) {
                throw new ApiException('posting date must not precede the original business date', 422);
            }

            // One reversal per original entry.
            $alreadyReversed = SettlementEntry::query()
                ->where('linked_entry_id', $original->id)
                ->where('source', SettlementEntry::SOURCE_REVERSAL)
                ->exists();
            if ($alreadyReversed) {
                throw ApiException::conflict('original entry has already been reversed', 'already_reversed');
            }

            $nowStr = Clock::now()->format('Y-m-d H:i:s.u');

            $reversal = new SettlementEntry();
            $reversal->fill([
                'contract_id' => $original->contract_id,
                'currency' => $original->currency,
                'business_date' => $postingDate,
                'external_txn_id' => 'REV-' . $original->external_txn_id,
                'amount' => bcsub('0', Money::normalize((string) $original->amount), Money::SCALE),
                'description' => 'reversal of entry #' . $original->id,
                'source' => SettlementEntry::SOURCE_REVERSAL,
                'linked_entry_id' => $original->id,
                'created_by' => $actor,
                'created_at' => $nowStr,
            ]);
            $reversal->save();

            $adjustmentRow = null;
            if ($delta !== null) {
                $adjustmentRow = new SettlementEntry();
                $adjustmentRow->fill([
                    'contract_id' => $original->contract_id,
                    'currency' => $original->currency,
                    'business_date' => $postingDate,
                    'external_txn_id' => 'ADJ-' . $original->external_txn_id,
                    'amount' => $delta,
                    'description' => 'adjustment of entry #' . $original->id,
                    'source' => SettlementEntry::SOURCE_ADJUSTMENT,
                    'linked_entry_id' => $original->id,
                    'created_by' => $actor,
                    'created_at' => $nowStr,
                ]);
                $adjustmentRow->save();
            }

            return [
                'reversal' => $this->present($reversal),
                'adjustment' => $adjustmentRow ? $this->present($adjustmentRow) : null,
            ];
        });
    }

    /** @return array<string,mixed> */
    private function present(SettlementEntry $e): array
    {
        return [
            'id' => (int) $e->id,
            'contract_id' => (int) $e->contract_id,
            'currency' => $e->currency,
            'business_date' => (string) $e->business_date,
            'external_txn_id' => $e->external_txn_id,
            'amount' => (string) $e->amount,
            'source' => $e->source,
            'linked_entry_id' => (int) $e->linked_entry_id,
        ];
    }
}
