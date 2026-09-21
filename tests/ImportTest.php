<?php

declare(strict_types=1);

namespace Meridian\Tests;

use Meridian\DomainException;
use Meridian\Models\DailyClose;
use Meridian\Models\ImportBatch;
use Meridian\Models\SettlementLine;
use Meridian\Services\ImportService;

final class ImportTest extends TestCase
{
    private ImportService $imports;

    protected function setUp(): void
    {
        parent::setUp();
        $this->imports = new ImportService();
    }

    /** @return array<int,array<string,mixed>> */
    private function lines(string $code, string $date = '2026-09-20'): array
    {
        return [
            ['external_ref' => 'TXN-1', 'contract_code' => $code, 'currency' => 'USD',
             'business_date' => $date, 'amount' => '100.25', 'description' => 'room nights'],
            ['external_ref' => 'TXN-2', 'contract_code' => $code, 'currency' => 'USD',
             'business_date' => $date, 'amount' => '-20.00', 'description' => 'cancellation'],
        ];
    }

    public function testImportCommitsBatchWithFixedPointAmounts(): void
    {
        [$contract] = $this->makeContract();
        $batch = $this->imports->import('file-1', $this->lines('CTR-T-1'), 'ops');

        $this->assertSame(2, $batch->inserted_count);
        $this->assertSame(0, $batch->duplicate_count);

        $line = SettlementLine::where('external_ref', 'TXN-1')->first();
        $this->assertSame(10025, (int) $line->amount_minor); // 100.25 USD -> minor units
        $this->assertSame($contract->id, $line->contract_id);
        $this->assertSame('2026-09-20', (string) $line->business_date);
    }

    public function testIdenticalReimportIsDeduplicated(): void
    {
        $this->makeContract();
        $this->imports->import('file-1', $this->lines('CTR-T-1'), 'ops');
        $batch2 = $this->imports->import('file-1-retry', $this->lines('CTR-T-1'), 'ops');

        $this->assertSame(0, $batch2->inserted_count);
        $this->assertSame(2, $batch2->duplicate_count);
        $this->assertSame(2, SettlementLine::count());
    }

    public function testSameRefWithDifferentContentConflictsAndRollsBackWholeBatch(): void
    {
        $this->makeContract();
        $this->imports->import('file-1', $this->lines('CTR-T-1'), 'ops');

        $tampered = $this->lines('CTR-T-1');
        $tampered[0]['amount'] = '999.99'; // same external_ref, different amount
        $tampered[] = ['external_ref' => 'TXN-3', 'contract_code' => 'CTR-T-1', 'currency' => 'USD',
                       'business_date' => '2026-09-20', 'amount' => '5.00'];

        $batchesBefore = ImportBatch::count();
        try {
            $this->imports->import('file-2', $tampered, 'ops');
            $this->fail('expected conflict');
        } catch (DomainException $e) {
            $this->assertSame('external_ref_conflict', $e->errorCode);
        }

        // Whole batch rolled back: no new batch row, no TXN-3 line.
        $this->assertSame($batchesBefore, ImportBatch::count());
        $this->assertNull(SettlementLine::where('external_ref', 'TXN-3')->first());
        $this->assertSame(2, SettlementLine::count());
    }

    public function testValidationFailureRollsBackEntireBatch(): void
    {
        $this->makeContract();
        $lines = $this->lines('CTR-T-1');
        $lines[1]['amount'] = 'not-a-number';

        $batchesBefore = ImportBatch::count();
        try {
            $this->imports->import('file-bad', $lines, 'ops');
            $this->fail('expected validation failure');
        } catch (DomainException $e) {
            $this->assertSame(422, $e->status);
        }
        $this->assertSame($batchesBefore, ImportBatch::count());
        $this->assertSame(0, SettlementLine::count());
    }

    public function testUnknownContractRejected(): void
    {
        $this->makeContract();
        $this->expectException(DomainException::class);
        $this->imports->import('file-x', $this->lines('NOPE'), 'ops');
    }

    public function testImportIntoClosedDateRejected(): void
    {
        $this->makeContract();
        DailyClose::create(['close_date' => '2026-09-20', 'closed_by' => 'finops']);

        $this->expectException(DomainException::class);
        $this->expectExceptionMessage('closed');
        $this->imports->import('file-late', $this->lines('CTR-T-1'), 'ops');
    }
}
