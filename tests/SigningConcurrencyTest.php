<?php

declare(strict_types=1);

namespace TravelOpsTests;

use DateTimeImmutable;
use TravelOps\Clock;
use TravelOps\Models\Signature;

final class SigningConcurrencyTest extends DbTestCase
{
    use BuildsContracts;

    public function testParallelConfirmationsNeverDoubleSign(): void
    {
        Clock::freeze(new DateTimeImmutable('2026-09-20 08:00:00'));
        $this->template = $this->makeTemplate();
        $c = $this->makeContract();
        // Single signer so all workers contend for exactly the same slot.
        $v = $this->initiate($c, ['a' => '1', 'b' => '2'], ['alice']);

        $jobs = [];
        for ($i = 0; $i < 6; $i++) {
            $jobs[] = [
                'task' => 'sign',
                'args' => [
                    'version_id' => (int) $v->id,
                    'signer' => 'alice',
                    'content_hash' => $v->content_hash,
                    'actor' => 'alice',
                    'freeze_clock' => '2026-09-20 09:00:00',
                ],
            ];
        }
        $results = $this->runParallel($jobs);

        $signatureIds = array_unique(array_map(
            fn ($r) => $r['signature_id'] ?? null,
            array_filter($results, fn ($r) => !empty($r['ok']))
        ));

        // Exactly one row exists and every accepted call returned that row.
        self::assertSame(1, Signature::query()->where('contract_version_id', $v->id)->count());
        self::assertCount(1, array_filter($signatureIds, fn ($id) => $id !== null));

        // Every rejected call was a benign duplicate/conflict, never a 500.
        foreach ($results as $r) {
            if (empty($r['ok'])) {
                self::assertSame('duplicate_signature', $r['code'] ?? null, json_encode($r));
            }
        }
    }

    public function testSigningAndWithdrawRaceEndsInExactlyOneValidState(): void
    {
        Clock::freeze(new DateTimeImmutable('2026-09-20 08:00:00'));
        $this->template = $this->makeTemplate();
        $c = $this->makeContract();
        $v = $this->initiate($c, ['a' => '1', 'b' => '2'], ['alice']);

        // Fire both transitions simultaneously across processes.
        $results = $this->runParallel([
            ['task' => 'sign', 'args' => [
                'version_id' => (int) $v->id, 'signer' => 'alice',
                'content_hash' => $v->content_hash,
                'freeze_clock' => '2026-09-20 09:00:00',
            ]],
            ['task' => 'withdraw', 'args' => [
                'version_id' => (int) $v->id, 'actor' => 'alice',
                'freeze_clock' => '2026-09-20 09:00:00',
            ]],
        ]);

        $statuses = array_map(fn ($r) => $r['ok'] ? 'ok' : ($r['code'] ?? 'error'), $results);
        $sigCount = Signature::query()->where('contract_version_id', $v->id)->count();
        $finalStatus = $v->fresh()->status;

        // Exactly one of the two transitions may win:
        //  - withdraw wins -> no signature row, version withdrawn;
        //  - sign wins     -> one signature row, single signer completes it -> signed.
        if ($finalStatus === 'withdrawn') {
            self::assertSame(0, $sigCount);
        } else {
            self::assertSame('signed', $finalStatus);
            self::assertSame(1, $sigCount);
        }
        self::assertContains($finalStatus, ['withdrawn', 'signed'], 'final=' . $finalStatus . ' results=' . json_encode($statuses));
    }
}
