<?php

declare(strict_types=1);

namespace Meridian\Tests;

use Meridian\DomainException;
use Meridian\Models\ContractEvent;
use Meridian\Models\ContractSigner;
use Meridian\Support\Clock;

final class SigningFlowTest extends TestCase
{
    public function testHappyPathOrderedSigning(): void
    {
        [$contract, $version] = $this->makeSigningContract();

        $r1 = $this->contracts->confirm($contract->id, 1, 1, 'alice');
        $this->assertFalse($r1['idempotent']);
        $this->assertSame('signing', $r1['version']->status);

        $r2 = $this->contracts->confirm($contract->id, 1, 2, 'bob');
        $this->assertSame('signed', $r2['version']->status);
        $this->assertSame('signed', $contract->fresh()->status);

        // Frozen content + digest recorded at initiation.
        $this->assertSame(hash('sha256', $version->content), $version->content_hash);
        $this->assertNotNull($version->sign_initiated_at);
        $this->assertNotNull($version->expires_at);

        // Operator + time recorded on each confirmation and in the event log.
        $signer1 = ContractSigner::where('version_id', $version->id)->where('seq', 1)->first();
        $this->assertSame('alice', $signer1->confirmed_by);
        $this->assertNotNull($signer1->confirmed_at);
        $actions = ContractEvent::where('contract_id', $contract->id)->pluck('action')->all();
        $this->assertContains('sign_initiated', $actions);
        $this->assertContains('confirmed', $actions);
        $this->assertContains('signed', $actions);
    }

    public function testOutOfOrderConfirmationRejected(): void
    {
        [$contract] = $this->makeSigningContract();
        $this->expectException(DomainException::class);
        $this->expectExceptionMessage('in order');
        $this->contracts->confirm($contract->id, 1, 2, 'bob');
    }

    public function testRetryOfConfirmationIsIdempotent(): void
    {
        [$contract] = $this->makeSigningContract();
        $first = $this->contracts->confirm($contract->id, 1, 1, 'alice');
        $retry = $this->contracts->confirm($contract->id, 1, 1, 'alice');

        $this->assertFalse($first['idempotent']);
        $this->assertTrue($retry['idempotent']);
        $this->assertSame(
            1,
            ContractSigner::where('version_id', $first['version']->id)->where('status', 'confirmed')->count()
        );
        // Only one confirm event was written.
        $this->assertSame(1, ContractEvent::where('contract_id', $contract->id)->where('action', 'confirmed')->count());
    }

    public function testWithdrawBeforeFirstSignature(): void
    {
        [$contract] = $this->makeSigningContract();
        $result = $this->contracts->withdraw($contract->id, 'ops');
        $this->assertSame('withdrawn', $result->status);

        // Confirming after withdrawal is rejected.
        $this->expectException(DomainException::class);
        $this->contracts->confirm($contract->id, 1, 1, 'alice');
    }

    public function testWithdrawAfterFirstSignatureRejected(): void
    {
        [$contract] = $this->makeSigningContract();
        $this->contracts->confirm($contract->id, 1, 1, 'alice');

        $this->expectException(DomainException::class);
        $this->expectExceptionMessage('first confirmation');
        $this->contracts->withdraw($contract->id, 'ops');
    }

    public function testSigningExpiresAfter72Hours(): void
    {
        Clock::setFixed(new \DateTimeImmutable('2026-09-20 10:00:00'));
        [$contract] = $this->makeSigningContract();

        Clock::setFixed(new \DateTimeImmutable('2026-09-23 10:00:01')); // 72h + 1s
        try {
            $this->contracts->confirm($contract->id, 1, 1, 'alice');
            $this->fail('expected expiry');
        } catch (DomainException $e) {
            $this->assertSame('expired', $e->errorCode);
        }
        $this->assertSame('expired', $contract->fresh()->status);
    }

    public function testConfirmJustBeforeDeadlineStillWorks(): void
    {
        Clock::setFixed(new \DateTimeImmutable('2026-09-20 10:00:00'));
        [$contract] = $this->makeSigningContract();

        Clock::setFixed(new \DateTimeImmutable('2026-09-23 09:59:59'));
        $r = $this->contracts->confirm($contract->id, 1, 1, 'alice');
        $this->assertFalse($r['idempotent']);
    }

    public function testContentChangeCreatesNewVersionAndVoidsOldConfirmations(): void
    {
        [$contract, $v1] = $this->makeSigningContract();
        $this->contracts->confirm($contract->id, 1, 1, 'alice'); // confirmation bound to v1

        $v2 = $this->contracts->createVersion($contract->id, [
            'contract_code' => 'CTR-T-1',
            'supplier_name' => 'Acme Supplies Ltd', // changed content
            'currency' => 'USD',
        ], 'editor');

        $this->assertSame(2, $v2->version_no);
        $this->assertSame('superseded', $v1->fresh()->status);
        $this->assertSame('draft', $contract->fresh()->status);

        // Old confirmation cannot be reused: v1 no longer accepts confirms,
        // and v2 starts with zero confirmations.
        try {
            $this->contracts->confirm($contract->id, 1, 2, 'bob');
            $this->fail('expected rejection on superseded version');
        } catch (DomainException $e) {
            $this->assertSame(409, $e->status);
        }
        $this->assertSame(0, ContractSigner::where('version_id', $v2->id)->count());

        // New version must go through its own full signing round.
        $this->contracts->initiateSign($contract->id, 2, [['name' => 'Alice'], ['name' => 'Bob']], 'ops');
        $this->contracts->confirm($contract->id, 2, 1, 'alice');
        $r = $this->contracts->confirm($contract->id, 2, 2, 'bob');
        $this->assertSame('signed', $r['version']->status);
        $this->assertNotSame($v1->content_hash, $v2->fresh()->content_hash);
    }

    public function testNewVersionRejectedAfterSigned(): void
    {
        [$contract] = $this->makeSignedContract();
        $this->expectException(DomainException::class);
        $this->contracts->createVersion($contract->id, [
            'contract_code' => 'CTR-T-1',
            'supplier_name' => 'Changed',
            'currency' => 'USD',
        ], 'editor');
    }

    public function testMissingTemplateVariableRejected(): void
    {
        $template = $this->makeTemplate();
        $this->expectException(DomainException::class);
        $this->contracts->createContract('CTR-BAD', $template->id, 'bad', [
            'contract_code' => 'CTR-BAD',
            // supplier_name and currency missing
        ], 'tester');
    }
}
