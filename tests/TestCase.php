<?php

declare(strict_types=1);

namespace Meridian\Tests;

use Illuminate\Database\Capsule\Manager as DB;
use Meridian\Models\Contract;
use Meridian\Models\ContractTemplate;
use Meridian\Models\ContractVersion;
use Meridian\Services\ContractService;
use Meridian\Support\Clock;
use Meridian\Support\Database;
use Meridian\Support\Migrator;
use PHPUnit\Framework\TestCase as BaseTestCase;

abstract class TestCase extends BaseTestCase
{
    protected ContractService $contracts;

    protected function setUp(): void
    {
        parent::setUp();
        Database::init();
        if ((getenv('DB_DRIVER') ?: 'sqlite') === 'mysql') {
            Database::waitForDb(10);
        }
        (new Migrator(dirname(__DIR__) . '/database/migrations'))->run();
        if ((getenv('DB_DRIVER') ?: 'sqlite') === 'mysql') {
            $this->truncateAll();
        }
        Clock::setFixed(null);
        $this->contracts = new ContractService();
    }

    private function truncateAll(): void
    {
        DB::statement('SET FOREIGN_KEY_CHECKS=0');
        foreach ([
            'contract_templates', 'contracts', 'contract_versions', 'contract_signers',
            'contract_events', 'import_batches', 'settlement_lines', 'allocation_rule_versions',
            'settlements', 'settlement_allocations', 'daily_closes', 'adjustments',
        ] as $table) {
            DB::table($table)->truncate();
        }
        DB::statement('SET FOREIGN_KEY_CHECKS=1');
    }

    protected function makeTemplate(): ContractTemplate
    {
        return $this->contracts->createTemplate(
            'test-template',
            'Contract {{contract_code}} between Meridian and {{supplier_name}}. Currency: {{currency}}.'
        );
    }

    /** @return array{0:Contract,1:ContractVersion} */
    protected function makeContract(string $code = 'CTR-T-1'): array
    {
        $template = $this->makeTemplate();
        $contract = $this->contracts->createContract($code, $template->id, 'Test contract', [
            'contract_code' => $code,
            'supplier_name' => 'Acme Supplies',
            'currency' => 'USD',
        ], 'tester');
        return [$contract, $contract->versions->first()];
    }

    /** @return array{0:Contract,1:ContractVersion} contract with signing initiated */
    protected function makeSigningContract(array $signers = [['name' => 'Alice'], ['name' => 'Bob']]): array
    {
        [$contract, $version] = $this->makeContract();
        $version = $this->contracts->initiateSign($contract->id, $version->version_no, $signers, 'ops');
        return [$contract->fresh(), $version];
    }

    /** @return array{0:Contract,1:ContractVersion} fully signed contract */
    protected function makeSignedContract(): array
    {
        [$contract, $version] = $this->makeSigningContract();
        $this->contracts->confirm($contract->id, $version->version_no, 1, 'alice');
        $this->contracts->confirm($contract->id, $version->version_no, 2, 'bob');
        return [$contract->fresh(), $version->fresh()];
    }

    protected static function isMysql(): bool
    {
        return (getenv('DB_DRIVER') ?: 'sqlite') === 'mysql';
    }

    protected function skipUnlessMysqlFork(): void
    {
        if (!self::isMysql() || !function_exists('pcntl_fork')) {
            $this->markTestSkipped('requires MySQL and pcntl');
        }
    }
}
