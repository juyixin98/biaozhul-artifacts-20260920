<?php

declare(strict_types=1);

use Meridian\Models\AllocationRuleVersion;
use Meridian\Models\ContractTemplate;
use Meridian\Services\ContractService;
use Meridian\Services\SettlementService;
use Meridian\Support\Database;
use Meridian\Support\Migrator;

require __DIR__ . '/../vendor/autoload.php';

Database::init();
Database::waitForDb();
(new Migrator(__DIR__ . '/../database/migrations'))->run();

// Idempotent sample data: a template, one contract, one allocation rule.
$template = ContractTemplate::firstOrCreate(
    ['name' => 'standard-supplier-settlement'],
    ['body' => <<<'TXT'
Meridian TravelOps Supplier Settlement Agreement
Contract: {{contract_code}}
Supplier: {{supplier_name}}
Hotel chain: {{hotel_chain}}
Settlement currency: {{currency}}
Commission rate: {{commission_rate}}%
TXT]
);

$contracts = new ContractService();
if (!Meridian\Models\Contract::where('code', 'CTR-2026-0001')->exists()) {
    $contracts->createContract('CTR-2026-0001', $template->id, 'Supplier settlement — Aurora Hotels', [
        'contract_code' => 'CTR-2026-0001',
        'supplier_name' => 'Aurora Hotels Group',
        'hotel_chain' => 'Aurora',
        'currency' => 'USD',
        'commission_rate' => '12.5',
    ], 'seed');
}

if (!AllocationRuleVersion::where('name', 'default-cost-split')->exists()) {
    (new SettlementService())->createRuleVersion('default-cost-split', [
        ['target' => 'ops', 'weight' => 60],
        ['target' => 'finance', 'weight' => 30],
        ['target' => 'support', 'weight' => 10],
    ], 'ops', 'seed');
}

echo "seed: ok\n";
