<?php

declare(strict_types=1);

namespace TravelOpsTests;

use TravelOps\ApiException;
use TravelOps\Models\Settlement;
use TravelOps\Models\SettlementEntryLink;
use TravelOps\Services\ImportService;
use TravelOps\Services\SettlementService;

final class SettlementBindingTest extends DbTestCase
{
    use BuildsContracts;

    private function entries(int $contractId): void
    {
        (new ImportService())->importBatch([[
            'contract_id' => $contractId,
            'currency' => 'EUR',
            'business_date' => '2026-09-18',
            'external_txn_id' => 'E-1',
            'amount' => '100.0000',
        ]], 'tester');
    }

    public function testSettlementRejectedWithoutFullySignedVersion(): void
    {
        $this->template = $this->makeTemplate();
        $c = $this->makeContract();
        $this->initiate($c, ['a' => '1', 'b' => '2'], ['alice']); // still pending
        $this->makeRules($c, [['target' => 'x', 'weight' => 1, 'remainder_owner' => true]]);
        $this->entries((int) $c->id);

        $this->expectException(ApiException::class);
        (new SettlementService())->create([
            'contract_id' => (int) $c->id,
            'currency' => 'EUR',
            'business_date' => '2026-09-18',
        ], 'tester');
    }

    public function testSettlementBindsVersionHashAndAllocates(): void
    {
        $this->template = $this->makeTemplate();
        $c = $this->makeContract();
        $v = $this->initiate($c, ['a' => '1', 'b' => '2'], ['alice']);
        $this->fullySign($v);
        $this->makeRules($c, [
            ['target' => 'hotel', 'weight' => 70, 'remainder_owner' => true],
            ['target' => 'fee', 'weight' => 30],
        ]);
        $this->entries((int) $c->id);

        $result = (new SettlementService())->create([
            'contract_id' => (int) $c->id,
            'currency' => 'EUR',
            'business_date' => '2026-09-18',
        ], 'tester');

        self::assertFalse($result['idempotent']);
        $s = $result['settlement'];
        self::assertSame((int) $v->id, $s['contract_version_id']);
        self::assertSame($v->content_hash, $s['content_hash']);
        self::assertSame('100.0000', $s['amount']);
        self::assertSame('70.0000', $s['allocations'][0]['allocated_amount']);
        self::assertSame('30.0000', $s['allocations'][1]['allocated_amount']);
    }

    public function testEntriesSettleOnlyOnce(): void
    {
        $this->template = $this->makeTemplate();
        $c = $this->makeContract();
        $v = $this->initiate($c, ['a' => '1', 'b' => '2'], ['alice']);
        $this->fullySign($v);
        $this->makeRules($c, [['target' => 'x', 'weight' => 1, 'remainder_owner' => true]]);
        $this->entries((int) $c->id);

        $filter = ['contract_id' => (int) $c->id, 'currency' => 'EUR', 'business_date' => '2026-09-18'];
        $first = (new SettlementService())->create($filter, 'tester');
        $replay = (new SettlementService())->create($filter, 'tester');

        self::assertTrue($replay['idempotent']);
        self::assertSame($first['settlement']['id'], $replay['settlement']['id']);
        self::assertSame(1, Settlement::query()->count());
        self::assertSame(1, SettlementEntryLink::query()->count());

        // Without an explicit idempotency key the deterministic key is the
        // grouping tuple, so a second settle with no fresh entries conflicts.
        $this->expectException(ApiException::class);
        (new SettlementService())->create($filter + ['idempotency_key' => 'force-new'], 'tester');
    }
}
