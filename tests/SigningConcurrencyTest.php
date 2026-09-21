<?php

declare(strict_types=1);

namespace Meridian\Tests;

use Meridian\DomainException;
use Meridian\Models\Contract;
use Meridian\Models\ContractEvent;
use Meridian\Models\ContractSigner;
use Meridian\Services\ContractService;
use Meridian\Support\Database;

/**
 * Race tests: run only on MySQL with pcntl (docker compose environment).
 * Each forked child gets its own PDO connection via Database::reconnect().
 */
final class SigningConcurrencyTest extends TestCase
{
    protected function setUp(): void
    {
        parent::setUp();
        $this->skipUnlessMysqlFork();
    }

    public function testConcurrentConfirmsOfSameSignerConfirmExactlyOnce(): void
    {
        [$contract, $version] = $this->makeSigningContract([['name' => 'Alice']]);
        $contractId = $contract->id;

        $pid = pcntl_fork();
        if ($pid === 0) {
            Database::reconnect();
            try {
                (new ContractService())->confirm($contractId, 1, 1, 'child-operator');
                exit(0);
            } catch (DomainException) {
                exit(1);
            }
        }

        $parentOk = true;
        try {
            $this->contracts->confirm($contractId, 1, 1, 'parent-operator');
        } catch (DomainException) {
            $parentOk = false;
        }

        pcntl_waitpid($pid, $status);
        $childOk = pcntl_wexitstatus($status) === 0;

        // Both calls may report success (the loser sees the idempotent retry
        // path), but the signer row must be confirmed exactly once, by one
        // operator, and only one 'confirmed' event may exist.
        $this->assertTrue($parentOk || $childOk);
        $signers = ContractSigner::where('version_id', $version->id)->where('status', 'confirmed')->get();
        $this->assertCount(1, $signers);
        $this->assertContains($signers->first()->confirmed_by, ['parent-operator', 'child-operator']);
        $this->assertSame(
            1,
            ContractEvent::where('contract_id', $contractId)->where('action', 'confirmed')->count()
        );
        $this->assertSame('signed', Contract::find($contractId)->status);
    }

    public function testSignVsWithdrawRaceResolvesToExactlyOneOutcome(): void
    {
        [$contract, $version] = $this->makeSigningContract([['name' => 'Alice'], ['name' => 'Bob']]);
        $contractId = $contract->id;

        $pid = pcntl_fork();
        if ($pid === 0) {
            Database::reconnect();
            try {
                (new ContractService())->withdraw($contractId, 'child-withdrawer');
                exit(0);
            } catch (DomainException) {
                exit(1);
            }
        }

        $confirmOk = true;
        try {
            $this->contracts->confirm($contractId, 1, 1, 'parent-signer');
        } catch (DomainException) {
            $confirmOk = false;
        }

        pcntl_waitpid($pid, $status);
        $withdrawOk = pcntl_wexitstatus($status) === 0;

        $final = Contract::find($contractId);
        $confirmedCount = ContractSigner::where('version_id', $version->id)->where('status', 'confirmed')->count();

        if ($withdrawOk) {
            // Withdrawal won: no confirmation may have slipped through.
            $this->assertSame('withdrawn', $final->status);
            $this->assertSame(0, $confirmedCount);
        } else {
            // Confirmation won: withdrawal must have been rejected.
            $this->assertTrue($confirmOk);
            $this->assertSame(1, $confirmedCount);
            $this->assertSame('signing', $final->status);
        }
    }
}
