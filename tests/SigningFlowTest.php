<?php

declare(strict_types=1);

namespace TravelOpsTests;

use DateTimeImmutable;
use TravelOps\ApiException;
use TravelOps\Clock;
use TravelOps\Models\Contract;
use TravelOps\Models\ContractVersion;
use TravelOps\Models\Signature;
use TravelOps\Services\SigningService;

final class SigningFlowTest extends DbTestCase
{
    use BuildsContracts;

    private SigningService $signing;

    protected function setUp(): void
    {
        parent::setUp();
        $this->signing = new SigningService();
    }

    public function testSigningMustFollowOrder(): void
    {
        $this->template = $this->makeTemplate();
        $c = $this->makeContract();
        $v = $this->initiate($c, ['a' => '1', 'b' => '2'], ['alice', 'bob']);

        $this->expectException(ApiException::class);
        $this->signing->sign((int) $v->id, 'bob', $v->content_hash, 'bob');
    }

    public function testFullOrderCompletesVersionAndActivatesContract(): void
    {
        $this->template = $this->makeTemplate();
        $c = $this->makeContract();
        $v = $this->initiate($c, ['a' => '1', 'b' => '2'], ['alice', 'bob']);

        $this->signing->sign((int) $v->id, 'alice', $v->content_hash, 'alice');
        self::assertSame('pending', $v->fresh()->status);

        $this->signing->sign((int) $v->id, 'bob', $v->content_hash, 'bob');
        self::assertSame('signed', $v->fresh()->status);
        self::assertSame('active', $c->fresh()->status);
    }

    public function testRetrySameConfirmationIsIdempotent(): void
    {
        $this->template = $this->makeTemplate();
        $c = $this->makeContract();
        $v = $this->initiate($c, ['a' => '1', 'b' => '2'], ['alice', 'bob']);

        $first = $this->signing->sign((int) $v->id, 'alice', $v->content_hash, 'alice');
        $retry = $this->signing->sign((int) $v->id, 'alice', $v->content_hash, 'alice');

        self::assertFalse($first['idempotent']);
        self::assertTrue($retry['idempotent']);
        self::assertSame((int) $first['signature']->id, (int) $retry['signature']->id);
        self::assertSame(1, Signature::query()->where('contract_version_id', $v->id)->where('signer', 'alice')->count());
    }

    public function testSigningWrongHashIsConflict(): void
    {
        $this->template = $this->makeTemplate();
        $c = $this->makeContract();
        $v = $this->initiate($c, ['a' => '1', 'b' => '2'], ['alice']);

        $this->expectException(ApiException::class);
        $this->signing->sign((int) $v->id, 'alice', str_repeat('0', 64), 'alice');
    }

    public function testWithdrawAllowedOnlyBeforeFirstSignature(): void
    {
        $this->template = $this->makeTemplate();
        $c = $this->makeContract();
        $v = $this->initiate($c, ['a' => '1', 'b' => '2'], ['alice', 'bob']);

        $this->signing->withdraw((int) $v->id, 'alice');
        self::assertSame('withdrawn', $v->fresh()->status);
        self::assertSame('withdrawn', $c->fresh()->status);

        // A withdrawn version cannot be signed afterwards.
        $this->expectException(ApiException::class);
        $this->signing->sign((int) $v->id, 'alice', $v->content_hash, 'alice');
    }

    public function testWithdrawAfterFirstSignatureRejected(): void
    {
        $this->template = $this->makeTemplate();
        $c = $this->makeContract();
        $v = $this->initiate($c, ['a' => '1', 'b' => '2'], ['alice', 'bob']);
        $this->signing->sign((int) $v->id, 'alice', $v->content_hash, 'alice');

        $this->expectException(ApiException::class);
        $this->signing->withdraw((int) $v->id, 'alice');
    }

    public function testExpiryBoundaryBlocksSigning(): void
    {
        $this->template = $this->makeTemplate();
        $c = $this->makeContract();
        $start = new DateTimeImmutable('2026-09-20 10:00:00');
        Clock::freeze($start);
        $v = $this->initiate($c, ['a' => '1', 'b' => '2'], ['alice']);

        // Still inside the window.
        Clock::freeze($start->modify('+71 hours 59 minutes 59 seconds'));
        $r = $this->signing->sign((int) $v->id, 'alice', $v->content_hash, 'alice');
        self::assertFalse($r['idempotent']);

        // A second pending version pushed past 72h must reject signing.
        Clock::freeze($start);
        $c2 = $this->makeContract('C-2');
        $v2 = $this->initiate($c2, ['a' => '1', 'b' => '2'], ['alice']);
        Clock::freeze($start->modify('+72 hours'));
        $this->expectException(ApiException::class);
        $this->signing->sign((int) $v2->id, 'alice', $v2->content_hash, 'alice');
    }

    public function testSweepMarksDueVersionsExpired(): void
    {
        $this->template = $this->makeTemplate();
        $c = $this->makeContract();
        $start = new DateTimeImmutable('2026-09-20 10:00:00');
        Clock::freeze($start);
        $v = $this->initiate($c, ['a' => '1', 'b' => '2'], ['alice']);

        Clock::freeze($start->modify('+72 hours 1 second'));
        $count = $this->signing->sweepExpired();
        self::assertGreaterThanOrEqual(1, $count);
        self::assertSame('expired', $v->fresh()->status);
        self::assertSame('expired', $c->fresh()->status);
    }

    public function testActorAndTimestampAreRecorded(): void
    {
        $this->template = $this->makeTemplate();
        $c = $this->makeContract();
        $v = $this->initiate($c, ['a' => '1', 'b' => '2'], ['alice']);
        $this->signing->sign((int) $v->id, 'alice', $v->content_hash, 'operator-42');

        $sig = Signature::query()->where('contract_version_id', $v->id)->first();
        self::assertSame('operator-42', $sig->signed_by);
        self::assertNotEmpty($sig->signed_at);
    }
}
