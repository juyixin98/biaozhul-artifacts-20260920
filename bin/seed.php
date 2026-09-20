<?php

declare(strict_types=1);

/**
 * Idempotent sample data:
 *  - a template + two contracts (one fully signed & active with rules, one pending)
 *  - a 70/30 allocation rule version with a declared remainder owner
 *  - one committed import batch of supplier settlement entries
 *  - no daily close (so the API walk-through can close a day itself)
 */

require __DIR__ . '/../vendor/autoload.php';

use TravelOps\Canonical;
use TravelOps\Clock;
use TravelOps\Database;
use TravelOps\Models\Contract;
use TravelOps\Models\ContractTemplate;
use TravelOps\Models\ContractVersion;
use TravelOps\Models\Signature;
use TravelOps\Models\SettlementEntry;
use TravelOps\Services\RuleService;

Database::boot();

$conn = Illuminate\Database\Capsule\Manager::connection();
$conn->transaction(function () use ($conn): void {
    if (ContractTemplate::query()->where('code', 'TPL-HOTEL')->exists()) {
        echo "[seed] sample data already present, nothing to do\n";
        return;
    }

    $now = Clock::now()->format('Y-m-d H:i:s.u');
    $actor = 'seeder';

    $template = new ContractTemplate();
    $template->fill([
        'code' => 'TPL-HOTEL',
        'name' => 'Hotel Allotment Agreement',
        'body_template' => "HOTEL ALLOTMENT AGREEMENT\n\nMeridian TravelOps and {{hotel_name}} agree on a room allotment for {{season}}.\nNegotiated nightly rate: {{currency}} {{rate}}.\nThe agreement covers {{room_nights}} room nights.",
        'version' => 1,
        'created_by' => $actor,
        'created_at' => $now,
        'updated_at' => $now,
    ]);
    $template->save();

    $makeContract = function (string $code, string $customer) use ($actor, $now): Contract {
        $c = new Contract();
        $c->fill([
            'code' => $code,
            'customer_name' => $customer,
            'status' => Contract::STATUS_DRAFT,
            'created_by' => $actor,
            'created_at' => $now,
            'updated_at' => $now,
        ]);
        $c->save();
        return $c;
    };

    $c1 = $makeContract('CT-2026-0001', 'Alpine Journey GmbH');
    $c2 = $makeContract('CT-2026-0002', 'Coastal Tours Ltd');

    $variables1 = [
        'hotel_name' => 'Grand Alpine',
        'season' => 'Winter 2026',
        'currency' => 'EUR',
        'rate' => '120.00',
        'room_nights' => '500',
    ];
    $rendered1 = preg_replace_callback(
        '/\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}/',
        static fn (array $m): string => (string) $variables1[$m[1]],
        $template->body_template
    );

    $v1 = new ContractVersion();
    $v1->fill([
        'contract_id' => $c1->id,
        'version_no' => 1,
        'template_id' => $template->id,
        'variables_json' => Canonical::json($variables1),
        'frozen_content' => $rendered1,
        'content_hash' => hash('sha256', $rendered1),
        'summary_json' => Canonical::json(['contract_code' => 'CT-2026-0001', 'season' => 'Winter 2026']),
        'signer_order' => json_encode(['procurement', 'finance'], JSON_UNESCAPED_UNICODE),
        'status' => ContractVersion::STATUS_SIGNED,
        'expires_at' => Clock::now()->modify('+72 hours')->format('Y-m-d H:i:s.u'),
        'initiated_by' => $actor,
        'initiated_at' => $now,
        'completed_at' => $now,
        'created_at' => $now,
        'updated_at' => $now,
    ]);
    $v1->save();

    foreach (['procurement' => 0, 'finance' => 1] as $signer => $pos) {
        $sig = new Signature();
        $sig->fill([
            'contract_version_id' => $v1->id,
            'signer' => $signer,
            'position' => $pos,
            'content_hash' => $v1->content_hash,
            'content_version' => 1,
            'status' => 'signed',
            'signed_by' => $signer,
            'signed_at' => $now,
            'created_at' => $now,
            'updated_at' => $now,
        ]);
        $sig->save();
    }

    $c1->fill(['status' => Contract::STATUS_ACTIVE, 'current_version_id' => $v1->id, 'updated_at' => $now]);
    $c1->save();

    (new RuleService())->createVersion((int) $c1->id, [
        ['target' => 'hotel-cost', 'weight' => 70, 'remainder_owner' => true],
        ['target' => 'service-fee', 'weight' => 30],
    ], $actor);

    $variables2 = [
        'hotel_name' => 'Sea Breeze Resort',
        'season' => 'Spring 2027',
        'currency' => 'USD',
        'rate' => '98.50',
        'room_nights' => '300',
    ];
    $rendered2 = preg_replace_callback(
        '/\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}/',
        static fn (array $m): string => (string) $variables2[$m[1]],
        $template->body_template
    );
    $v2 = new ContractVersion();
    $v2->fill([
        'contract_id' => $c2->id,
        'version_no' => 1,
        'template_id' => $template->id,
        'variables_json' => Canonical::json($variables2),
        'frozen_content' => $rendered2,
        'content_hash' => hash('sha256', $rendered2),
        'summary_json' => Canonical::json(['contract_code' => 'CT-2026-0002', 'season' => 'Spring 2027']),
        'signer_order' => json_encode(['procurement', 'legal', 'finance'], JSON_UNESCAPED_UNICODE),
        'status' => ContractVersion::STATUS_PENDING,
        'expires_at' => Clock::now()->modify('+72 hours')->format('Y-m-d H:i:s.u'),
        'initiated_by' => $actor,
        'initiated_at' => $now,
        'created_at' => $now,
        'updated_at' => $now,
    ]);
    $v2->save();
    $c2->fill(['status' => Contract::STATUS_PENDING, 'current_version_id' => $v2->id, 'updated_at' => $now]);
    $c2->save();

    $sampleRows = [
        [1, 'EUR', '2026-09-18', 'SUP-1001', '100.0000', 'invoice line A'],
        [1, 'EUR', '2026-09-18', 'SUP-1002', '0.0100', 'rounding probe'],
        [1, 'EUR', '2026-09-19', 'SUP-1003', '250.5500', 'invoice line B'],
        [1, 'EUR', '2026-09-19', 'SUP-1004', '9.9900', 'minibar pass-through'],
    ];
    foreach ($sampleRows as [$contractId, $currency, $date, $txn, $amount, $desc]) {
        $e = new SettlementEntry();
        $e->fill([
            'contract_id' => $contractId,
            'currency' => $currency,
            'business_date' => $date,
            'external_txn_id' => $txn,
            'amount' => $amount,
            'description' => $desc,
            'source' => SettlementEntry::SOURCE_IMPORT,
            'created_by' => $actor,
            'created_at' => $now,
        ]);
        $e->save();
    }

    echo "[seed] inserted template, 2 contracts (1 signed, 1 pending), rules and 4 entries\n";
});
