<?php

declare(strict_types=1);

namespace Meridian\Tests;

use Meridian\DomainException;
use Meridian\Models\Adjustment;
use Meridian\Models\DailyClose;
use Meridian\Models\ImportBatch;
use Meridian\Models\SettlementLine;
use Meridian\Services\CloseService;
use Meridian\Services\ImportService;
use Meridian\Support\Database;

final class CloseTest extends TestCase
{
    private CloseService $closes;
    private ImportService $imports;

    protected function setUp(): void
    {
        parent::setUp();
        $this->closes = new CloseService();
        $this->imports = new ImportService();
    }

    private function importOne(string $ref, string $date = '2026-09-20'): SettlementLine
    {
        $this->makeContract();
        $this->imports->import('src', [[
            'external_ref' => $ref, 'contract_code' => 'CTR-T-1', 'currency' => 'USD',
            'business_date' => $date, 'amount' => '42.00',
        ]], 'ops');
        return SettlementLine::where('external_ref', $ref)->first();
    }

    public function testCloseIsIdempotent(): void
    {
        $c1 = $this->closes->close('2026-09-20', 'finops');
        $c2 = $this->closes->close('2026-09-20', 'finops');
        $this->assertSame($c1->id, $c2->id);
        $this->assertSame(1, DailyClose::count());
    }

    public function testClosedDayBlocksImports(): void
    {
        $this->makeContract();
        $this->closes->close('2026-09-20', 'finops');

        $this->expectException(DomainException::class);
        $this->expectExceptionMessage('closed');
        $this->imports->import('late', [[
            'external_ref' => 'TXN-LATE', 'contract_code' => 'CTR-T-1', 'currency' => 'USD',
            'business_date' => '2026-09-20', 'amount' => '1.00',
        ]], 'ops');
    }

    public function testReversalPostsToNextOpenDayWithoutTouchingOriginal(): void
    {
        $line = $this->importOne('TXN-1');
        $this->closes->close('2026-09-20', 'finops');

        $adj = $this->closes->adjust('line', $line->id, 'reversal', null, '2026-09-21', 'wrong amount', 'finops');

        $this->assertSame(-4200, (int) $adj->amount_minor); // exact negation of original
        $this->assertSame('2026-09-21', (string) $adj->business_date);
        // Original record untouched.
        $this->assertSame(4200, (int) $line->fresh()->amount_minor);
        $this->assertSame('active', $line->fresh()->status);
    }

    public function testAdjustmentAmountOnOpenDay(): void
    {
        $line = $this->importOne('TXN-1');
        $this->closes->close('2026-09-20', 'finops');

        $adj = $this->closes->adjust('line', $line->id, 'adjustment', '2.50', '2026-09-21', 'price fix', 'finops');
        $this->assertSame(250, (int) $adj->amount_minor);
    }

    public function testCorrectionCannotTargetClosedOrPastDay(): void
    {
        $line = $this->importOne('TXN-1');
        $this->closes->close('2026-09-20', 'finops');
        $this->closes->close('2026-09-21', 'finops');

        // Closed correction day.
        try {
            $this->closes->adjust('line', $line->id, 'reversal', null, '2026-09-21', 'x', 'finops');
            $this->fail('expected closed-day rejection');
        } catch (DomainException $e) {
            $this->assertSame('date_closed', $e->errorCode);
        }
        // Same/past day.
        $this->expectException(DomainException::class);
        $this->closes->adjust('line', $line->id, 'reversal', null, '2026-09-19', 'x', 'finops');
    }

    public function testCloseVsImportRaceLeavesNoHalfClosedDay(): void
    {
        if (!self::isMysql() || !function_exists('pcntl_fork')) {
            $this->markTestSkipped('requires MySQL and pcntl');
        }
        $this->makeContract();

        $pid = pcntl_fork();
        if ($pid === 0) {
            Database::reconnect();
            try {
                (new ImportService())->import('racer', [[
                    'external_ref' => 'TXN-RACE', 'contract_code' => 'CTR-T-1', 'currency' => 'USD',
                    'business_date' => '2026-09-20', 'amount' => '7.00',
                ]], 'ops');
                exit(0);
            } catch (DomainException) {
                exit(1);
            }
        }

        usleep(50_000); // let the child get going first half the time
        $this->closes->close('2026-09-20', 'finops');

        pcntl_waitpid($pid, $status);
        $importOk = pcntl_wexitstatus($status) === 0;

        $line = SettlementLine::where('external_ref', 'TXN-RACE')->first();
        if ($importOk) {
            // Import committed fully before the close: line and batch both exist.
            $this->assertNotNull($line);
            $this->assertSame(1, ImportBatch::where('source', 'racer')->count());
        } else {
            // Close won: nothing from the import may be present (no half state).
            $this->assertNull($line);
            $this->assertSame(0, ImportBatch::where('source', 'racer')->count());
        }
        $this->assertSame(1, DailyClose::where('close_date', '2026-09-20')->count());
    }
}
