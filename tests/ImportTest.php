<?php

declare(strict_types=1);

namespace TravelOpsTests;

use TravelOps\ApiException;
use TravelOps\Models\DailyClose;
use TravelOps\Models\SettlementEntry;
use TravelOps\Services\CloseService;
use TravelOps\Services\ImportService;

final class ImportTest extends DbTestCase
{
    use BuildsContracts;

    private ImportService $imports;
    private int $contractId;

    protected function setUp(): void
    {
        parent::setUp();
        $this->imports = new ImportService();
        $this->template = $this->makeTemplate();
        $this->contractId = (int) $this->makeContract()->id;
    }

    private function row(string $txn, string $amount, string $date = '2026-09-18', string $currency = 'EUR'): array
    {
        return [
            'contract_id' => $this->contractId,
            'currency' => $currency,
            'business_date' => $date,
            'external_txn_id' => $txn,
            'amount' => $amount,
        ];
    }

    public function testBatchImportsAndAggregatesByContractCurrencyDate(): void
    {
        $this->imports->importBatch([
            $this->row('T1', '100.0000'),
            $this->row('T2', '50.2500'),
            $this->row('T3', '10.0000', '2026-09-19'),
            $this->row('T4', '5.0000', '2026-09-18', 'USD'),
        ], 'tester');

        $groups = $this->imports->aggregation($this->contractId);
        $byKey = [];
        foreach ($groups as $g) {
            $byKey[$g['currency'] . ':' . $g['business_date']] = $g;
        }
        self::assertSame('150.2500', $byKey['EUR:2026-09-18']['total_amount']);
        self::assertSame(2, $byKey['EUR:2026-09-18']['row_count']);
        self::assertSame('10.0000', $byKey['EUR:2026-09-19']['total_amount']);
        self::assertSame('5.0000', $byKey['USD:2026-09-18']['total_amount']);
    }

    public function testDuplicateIdSameContentIsSkippedNotDoubled(): void
    {
        $this->imports->importBatch([$this->row('DUP', '100.0000')], 'tester');
        $result = $this->imports->importBatch([$this->row('DUP', '100.0000')], 'tester');

        self::assertSame(1, $result['duplicates']);
        self::assertSame(0, $result['imported']);
        self::assertSame(1, SettlementEntry::query()->where('external_txn_id', 'DUP')->count());
    }

    public function testDuplicateIdChangedContentIsConflict(): void
    {
        $this->imports->importBatch([$this->row('DUP', '100.0000')], 'tester');

        $this->expectException(ApiException::class);
        $this->imports->importBatch([$this->row('DUP', '101.0000')], 'tester');
    }

    public function testAnyInvalidRowRollsBackTheEntireBatch(): void
    {
        $before = SettlementEntry::query()->count();
        try {
            $this->imports->importBatch([
                $this->row('OK1', '10.0000'),
                $this->row('BAD', 'not-a-number'),
                $this->row('OK2', '20.0000'),
            ], 'tester');
            self::fail('expected validation failure');
        } catch (ApiException) {
        }
        // Even the valid rows of the bad batch must not persist.
        self::assertSame($before, SettlementEntry::query()->count());
        self::assertFalse(SettlementEntry::query()->where('external_txn_id', 'OK1')->exists());
    }

    public function testFloatAndExcessPrecisionAreRejected(): void
    {
        $this->expectException(ApiException::class);
        $this->imports->importBatch([[
            'contract_id' => $this->contractId,
            'currency' => 'EUR',
            'business_date' => '2026-09-18',
            'external_txn_id' => 'F1',
            'amount' => 12.34567, // JSON float -> never accepted
        ]], 'tester');
    }

    public function testImportAfterCloseIsRejected(): void
    {
        (new CloseService())->close('2026-09-18', 'tester');
        $this->expectException(ApiException::class);
        $this->imports->importBatch([$this->row('LATE', '10.0000')], 'tester');
    }
}
