<?php

declare(strict_types=1);

namespace Meridian\Tests;

use Meridian\DomainException;
use Meridian\Models\Settlement;
use Meridian\Services\ImportService;
use Meridian\Services\SettlementService;

final class SettlementBindingTest extends TestCase
{
    private SettlementService $settlements;
    private ImportService $imports;

    protected function setUp(): void
    {
        parent::setUp();
        $this->settlements = new SettlementService();
        $this->imports = new ImportService();
    }

    private function makeRule(): int
    {
        $rule = $this->settlements->createRuleVersion('test-split', [
            ['target' => 'ops', 'weight' => 1],
            ['target' => 'fin', 'weight' => 1],
            ['target' => 'sup', 'weight' => 1],
        ], 'sup', 'tester');
        return $rule->id;
    }

    private function importLines(string $code, string $amount, string $ref): void
    {
        $this->imports->import('src', [[
            'external_ref' => $ref, 'contract_code' => $code, 'currency' => 'USD',
            'business_date' => '2026-09-20', 'amount' => $amount,
        ]], 'ops');
    }

    public function testSettlementRequiresFullySignedVersion(): void
    {
        [$contract] = $this->makeSigningContract(); // signing, not signed
        $this->importLines('CTR-T-1', '100.00', 'TXN-A');
        $ruleId = $this->makeRule();

        $this->expectException(DomainException::class);
        $this->expectExceptionMessage('signed');
        $this->settlements->generate($contract->id, null, 'USD', '2026-09-20', $ruleId, 'ops');
    }

    public function testSettlementBindsSignedVersionAndAllocationsBalance(): void
    {
        [$contract, $version] = $this->makeSignedContract();
        $this->importLines('CTR-T-1', '100.00', 'TXN-A'); // 10000 minor
        $ruleId = $this->makeRule();

        $s = $this->settlements->generate($contract->id, null, 'USD', '2026-09-20', $ruleId, 'ops');

        $this->assertSame($version->id, $s->contract_version_id);
        $this->assertSame(10000, (int) $s->total_minor);

        $allocs = $s->allocations->sortBy('target')->values();
        $this->assertSame(10000, (int) $allocs->sum('amount_minor')); // 分摊总和 == 原金额
        $sink = $allocs->firstWhere('is_remainder_sink', true);
        $this->assertSame('sup', $sink->target);              // 尾差归属明确
        $this->assertSame(3334, (int) $sink->amount_minor);   // 3333+3333+3334
    }

    public function testDuplicateSettlementRequestRejected(): void
    {
        [$contract] = $this->makeSignedContract();
        $this->importLines('CTR-T-1', '10.00', 'TXN-A');
        $ruleId = $this->makeRule();

        $this->settlements->generate($contract->id, null, 'USD', '2026-09-20', $ruleId, 'ops');

        $this->expectException(DomainException::class);
        $this->expectExceptionMessage('already exists');
        $this->settlements->generate($contract->id, null, 'USD', '2026-09-20', $ruleId, 'ops');
    }

    public function testSettlementPinnedToExplicitVersion(): void
    {
        [$contract, $version] = $this->makeSignedContract();
        $this->importLines('CTR-T-1', '10.00', 'TXN-A');
        $ruleId = $this->makeRule();

        $s = $this->settlements->generate($contract->id, $version->id, 'USD', '2026-09-20', $ruleId, 'ops');
        $this->assertSame($version->id, $s->contract_version_id);
    }

    public function testRuleVersionsAreImmutableAndVersioned(): void
    {
        $v1 = $this->settlements->createRuleVersion('r', [['target' => 'a', 'weight' => 1]], null, 'tester');
        $v2 = $this->settlements->createRuleVersion('r', [
            ['target' => 'a', 'weight' => 1],
            ['target' => 'b', 'weight' => 1],
        ], null, 'tester');

        $this->assertSame(1, $v1->version_no);
        $this->assertSame(2, $v2->version_no);
        $this->assertSame(1, \Meridian\Models\AllocationRuleVersion::where('name', 'r')->where('version_no', 1)->count());
    }
}
