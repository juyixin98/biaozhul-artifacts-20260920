<?php

declare(strict_types=1);

namespace TravelOps\Services;

use TravelOps\ApiException;
use TravelOps\Canonical;
use TravelOps\Clock;
use TravelOps\Money;
use TravelOps\Models\DailyClose;

/**
 * Daily close.
 *
 * Closing a date and importing into it take the SAME per-date advisory lock
 * ('bizdate:<date>') inside their transactions, so exactly one ordering wins:
 *   - import commits first  -> close counts the new rows (nothing missed);
 *   - close commits first   -> import is rejected with date_closed (it can
 *                              never slip in behind a close / half-close).
 * The close row itself is protected by UNIQUE(business_date), so a concurrent
 * duplicate close can never insert twice.
 */
final class CloseService
{
    /** @return array{date:string, entry_count:int, totals:array<string,string>, idempotent:bool} */
    public function close(string $date, string $actor): array
    {
        self::assertDate($date);

        return ImportService::withDateLocks([$date], function () use ($date, $actor): array {
            $existing = DailyClose::query()->where('business_date', $date)->first();
            if ($existing) {
                // Closing an already-closed date is an idempotent report-back.
                return [
                    'date' => $date,
                    'entry_count' => (int) $existing->entry_count,
                    'totals' => json_decode((string) $existing->total_amount_json, true, 512, JSON_THROW_ON_ERROR),
                    'idempotent' => true,
                ];
            }

            // Snapshot of everything that committed before this point — the
            // advisory lock guarantees no import is in flight concurrently.
            $rows = \Illuminate\Database\Capsule\Manager::connection()->table('settlement_entries')
                ->select('currency')
                ->selectRaw('COUNT(*) AS c, SUM(amount) AS t')
                ->where('business_date', $date)
                ->groupBy('currency')
                ->get();

            $totals = [];
            $count = 0;
            foreach ($rows as $r) {
                $totals[$r->currency] = Money::normalize((string) $r->t);
                $count += (int) $r->c;
            }

            $close = new DailyClose();
            try {
                $close->fill([
                    'business_date' => $date,
                    'status' => 'closed',
                    'entry_count' => $count,
                    'total_amount_json' => Canonical::json($totals),
                    'closed_by' => $actor,
                    'closed_at' => Clock::now()->format('Y-m-d H:i:s.u'),
                ]);
                $close->save();
            } catch (\Illuminate\Database\QueryException $e) {
                if ($e->getCode() === '23000') {
                    throw ApiException::conflict("date {$date} was closed concurrently", 'already_closed');
                }
                throw $e;
            }

            return ['date' => $date, 'entry_count' => $count, 'totals' => $totals, 'idempotent' => false];
        });
    }

    public function isClosed(string $date): bool
    {
        return DailyClose::query()->where('business_date', $date)->exists();
    }

    /** @return array<int,array<string,mixed>> */
    public function listCloses(): array
    {
        return array_map(static fn (DailyClose $c) => [
            'business_date' => (string) $c->business_date,
            'entry_count' => (int) $c->entry_count,
            'total_amount' => json_decode((string) $c->total_amount_json, true, 512, JSON_THROW_ON_ERROR),
            'closed_by' => $c->closed_by,
            'closed_at' => $c->closed_at,
        ], DailyClose::query()->orderBy('business_date')->get()->all());
    }

    public static function assertDate(string $date): void
    {
        if (!preg_match('/^\d{4}-\d{2}-\d{2}$/', $date) ||
            !checkdate((int) substr($date, 5, 2), (int) substr($date, 8, 2), (int) substr($date, 0, 4))) {
            throw new ApiException('business_date must be YYYY-MM-DD', 422);
        }
    }
}
