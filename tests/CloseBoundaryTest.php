<?php

declare(strict_types=1);

namespace TravelOpsTests;

use DateTimeImmutable;
use TravelOps\Clock;
use TravelOps\Models\DailyClose;
use TravelOps\Models\SettlementEntry;
use TravelOps\ApiException;

final class CloseBoundaryTest extends DbTestCase
{
    use BuildsContracts;

    private int $contractId;

    protected function setUp(): void
    {
        parent::setUp();
        Clock::freeze(new DateTimeImmutable('2026-09-18 12:00:00'));
        $this->template = $this->makeTemplate();
        $this->contractId = (int) $this->makeContract()->id;
    }

    private function entryRow(string $txn, string $amount = '10.0000'): array
    {
        return [
            'contract_id' => $this->contractId,
            'currency' => 'EUR',
            'business_date' => '2026-09-18',
            'external_txn_id' => $txn,
            'amount' => $amount,
        ];
    }

    public function testCloseSnapshotsEntriesAndMakesDateImmutable(): void
    {
        (new \TravelOps\Services\ImportService())->importBatch([
            $this->entryRow('C1', '10.0000'),
            $this->entryRow('C2', '15.5000'),
        ], 'tester');

        $result = (new \TravelOps\Services\CloseService())->close('2026-09-18', 'closer');
        self::assertSame(2, $result['entry_count']);
        self::assertSame('25.5000', $result['totals']['EUR']);

        $close = DailyClose::query()->where('business_date', '2026-09-18')->first();
        self::assertSame('closer', $close->closed_by);
        self::assertNotEmpty($close->closed_at);

        // Idempotent re-close reports the same snapshot.
        $again = (new \TravelOps\Services\CloseService())->close('2026-09-18', 'closer');
        self::assertTrue($again['idempotent']);
        self::assertSame(2, $again['entry_count']);
    }

    public function testParallelImportAndCloseNeverLosesAnEntryOrHalfCloses(): void
    {
        // Rows 1-5 land first so the close always has *something*.
        (new \TravelOps\Services\ImportService())->importBatch([
            $this->entryRow('BASE-1', '1.0000'),
            $this->entryRow('BASE-2', '2.0000'),
            $this->entryRow('BASE-3', '3.0000'),
            $this->entryRow('BASE-4', '4.0000'),
            $this->entryRow('BASE-5', '5.0000'),
        ], 'tester');

        $racerEntries = [];
        for ($i = 0; $i < 12; $i++) {
            $racerEntries[] = ['task' => 'import', 'args' => [
                'entries' => [$this->entryRow('RACE-' . $i, '1.0000')],
                'freeze_clock' => '2026-09-18 12:0' . ($i % 10) . ':00',
            ]];
        }
        $closeJobs = [];
        for ($i = 0; $i < 4; $i++) {
            $closeJobs[] = ['task' => 'close', 'args' => [
                'business_date' => '2026-09-18',
                'freeze_clock' => '2026-09-18 13:00:00',
            ]];
        }

        // Launch imports and closes together so they truly contend.
        $results = $this->runParallel(array_merge($racerEntries, $closeJobs));

        $acceptedImports = 0;
        $rejectedImports = 0;
        $closeSnapshots = [];
        foreach ($results as $r) {
            if (isset($r['entry_count'])) {
                $closeSnapshots[] = $r['entry_count'];
            } elseif (!empty($r['ok'])) {
                $acceptedImports += $r['imported'];
            } else {
                self::assertSame('date_closed', $r['code'] ?? null, json_encode($r));
                $rejectedImports++;
            }
        }

        // Exactly one close row exists (no double/half close).
        self::assertSame(1, DailyClose::query()->where('business_date', '2026-09-18')->count());

        $closedCount = (int) DailyClose::query()->where('business_date', '2026-09-18')->value('entry_count');
        $physicalCount = (int) SettlementEntry::query()->where('business_date', '2026-09-18')->count();

        // Conservation, serialized by the per-date advisory lock:
        //   - an import that commits BEFORE the close is in the snapshot;
        //   - an import after the close is rejected and inserts nothing;
        // therefore every physical row is counted once and the close snapshot
        // equals the physical total (no missed entry, no half-close).
        self::assertSame(5 + $acceptedImports, $physicalCount);
        self::assertSame($physicalCount, $closedCount);
        self::assertSame(12, $acceptedImports + $rejectedImports);
        self::assertGreaterThanOrEqual(1, count($closeSnapshots));
        foreach ($closeSnapshots as $n) {
            self::assertSame($closedCount, $n, 'idempotent close snapshots must agree');
        }
    }

    public function testCorrectionAfterClosePostsToNextOpenDay(): void
    {
        (new \TravelOps\Services\ImportService())->importBatch([$this->entryRow('ORIG', '100.0000')], 'tester');
        (new \TravelOps\Services\CloseService())->close('2026-09-18', 'closer');

        $original = SettlementEntry::query()->where('external_txn_id', 'ORIG')->first();
        $result = (new \TravelOps\Services\CorrectionService())->reverse(
            (int) $original->id,
            '2026-09-19',
            'tester',
            '5.0000'
        );

        self::assertSame('-100.0000', $result['reversal']['amount']);
        self::assertSame('5.0000', $result['adjustment']['amount']);
        self::assertSame('2026-09-19', $result['reversal']['business_date']);
        self::assertSame((int) $original->id, $result['reversal']['linked_entry_id']);

        // Original row is untouched.
        self::assertSame('100.0000', (string) $original->fresh()->amount);

        // The net movement on the 19th aggregates to -95.
        $groups = (new \TravelOps\Services\ImportService())->aggregation($this->contractId);
        $net = null;
        foreach ($groups as $g) {
            if ($g['business_date'] === '2026-09-19') {
                $net = $g['total_amount'];
            }
        }
        self::assertSame('-95.0000', $net);
    }

    public function testCannotCorrectOntoAClosedDay(): void
    {
        (new \TravelOps\Services\ImportService())->importBatch([$this->entryRow('ORIG2', '100.0000')], 'tester');
        (new \TravelOps\Services\CloseService())->close('2026-09-18', 'closer');
        (new \TravelOps\Services\CloseService())->close('2026-09-19', 'closer');

        $original = SettlementEntry::query()->where('external_txn_id', 'ORIG2')->first();
        $this->expectException(ApiException::class);
        (new \TravelOps\Services\CorrectionService())->reverse((int) $original->id, '2026-09-19', 'tester');
    }

    public function testReversalCanOnlyHappenOnce(): void
    {
        (new \TravelOps\Services\ImportService())->importBatch([$this->entryRow('ORIG3', '10.0000')], 'tester');
        (new \TravelOps\Services\CloseService())->close('2026-09-18', 'closer');

        $original = SettlementEntry::query()->where('external_txn_id', 'ORIG3')->first();
        $svc = new \TravelOps\Services\CorrectionService();
        $svc->reverse((int) $original->id, '2026-09-19', 'tester');

        $this->expectException(ApiException::class);
        $svc->reverse((int) $original->id, '2026-09-20', 'tester');
    }
}
