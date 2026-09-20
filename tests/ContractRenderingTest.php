<?php

declare(strict_types=1);

namespace TravelOpsTests;

use TravelOps\ApiException;
use TravelOps\Services\ContractService;

final class ContractRenderingTest extends DbTestCase
{
    use BuildsContracts;

    public function testInitiatingFreezesContentSummaryAndHash(): void
    {
        $this->template = $this->makeTemplate('TPL-X', 'Rate {{rate}} for {{season}}.');
        $c = $this->makeContract('C-X');
        $v = $this->initiate($c, ['rate' => '120.00', 'season' => 'Winter'], ['alice']);

        self::assertSame('Rate 120.00 for Winter.', $v->frozen_content);
        self::assertSame(hash('sha256', 'Rate 120.00 for Winter.'), $v->content_hash);
        $summary = $v->summary();
        self::assertSame('Winter', $summary['variables']['season']);
        self::assertSame(1, $v->version_no);
    }

    public function testMissingVariableIsRejected(): void
    {
        $this->template = $this->makeTemplate('TPL-Y', 'A={{a}} B={{b}}');
        $c = $this->makeContract('C-Y');
        $this->expectException(ApiException::class);
        $this->initiate($c, ['a' => '1'], ['alice']);
    }

    public function testContentChangeCreatesNewVersionNotInPlaceEdit(): void
    {
        $this->template = $this->makeTemplate('TPL-Z', 'A={{a}}');
        $c = $this->makeContract('C-Z');
        $v1 = $this->initiate($c, ['a' => '1'], ['alice']);
        $this->fullySign($v1);

        // A new deal on the same contract: new version, fresh pending state.
        $v2 = $this->initiate($c, ['a' => '2'], ['alice']);
        self::assertSame(2, (int) $v2->version_no);
        self::assertSame('pending', $v2->status);
        self::assertNotSame($v1->content_hash, $v2->content_hash);

        // The old version keeps its own signature bound to the old content.
        $oldSig = \TravelOps\Models\Signature::query()->where('contract_version_id', $v1->id)->first();
        self::assertSame($v1->content_hash, $oldSig->content_hash);
        self::assertSame(1, (int) $oldSig->content_version);
    }

    public function testCannotInitiateWhilePendingVersionOpen(): void
    {
        $this->template = $this->makeTemplate('TPL-W', 'A={{a}}');
        $c = $this->makeContract('C-W');
        $this->initiate($c, ['a' => '1'], ['alice']);
        $this->expectException(ApiException::class);
        $this->initiate($c, ['a' => '2'], ['alice']);
    }
}
