<?php

declare(strict_types=1);

namespace TravelOps\Services;

use Illuminate\Database\Capsule\Manager as Capsule;
use TravelOps\ApiException;
use TravelOps\Clock;
use TravelOps\Models\Contract;
use TravelOps\Models\ContractVersion;
use TravelOps\Models\Signature;

/**
 * Ordered signing, withdrawal and 72-hour expiry.
 *
 * Concurrency strategy
 * --------------------
 * Every mutation runs in one transaction that takes SELECT ... FOR UPDATE on
 * the contract row (a common lock point), then re-reads the version and its
 * signatures. Signatures are protected by a UNIQUE(version, signer) key so a
 * retry — even one that races past the row lock — can never double-confirm.
 *
 * "Valid state only" transitions: a signer may confirm exactly when the
 * version is pending, the 72h window has not elapsed, all earlier positions
 * are signed, and the supplied content hash matches the frozen content.
 */
final class SigningService
{
    /**
     * Confirm a signature for $signer against $expectedHash.
     *
     * Idempotent retry semantics: repeating the SAME (version, signer, hash)
     * confirmation returns the stored signature (HTTP 200). A retry that
     * targets different content is a 409 conflict — a confirmation is bound
     * to one concrete version and can never be silently re-bound.
     *
     * @return array{signature:Signature, idempotent:bool}
     */
    public function sign(int $versionId, string $signer, string $expectedHash, string $actor): array
    {
        if ($expectedHash === '') {
            throw new ApiException('content_hash is required', 422);
        }

        $idempotent = false;
        $signature = \TravelOps\Locks::withTransactionalLock(
            'sign',
            (string) $versionId,
            function () use ($versionId, $signer, $expectedHash, $actor, &$idempotent): Signature {
                $version = $this->lockedVersion($versionId);

                // The confirmation is always bound to this exact frozen
                // content, regardless of the version's current state.
                if (!hash_equals($version->content_hash, $expectedHash)) {
                    throw ApiException::conflict(
                        'content hash does not match the frozen version content',
                        'content_mismatch',
                        ['expected' => $version->content_hash]
                    );
                }

                // A retry of the same (version, signer) confirmation is
                // idempotent even after the version completed — checked before
                // the pending-state guard below.
                $existing = Signature::query()
                    ->where('contract_version_id', $version->id)
                    ->where('signer', $signer)
                    ->first();
                if ($existing) {
                    if (!hash_equals($existing->content_hash, $expectedHash)) {
                        throw ApiException::conflict(
                            'signer already confirmed different content; confirmation cannot be re-bound',
                            'content_rebound'
                        );
                    }
                    $idempotent = true;
                    return $existing;
                }

                // Only a pending, unexpired version can take a NEW signature.
                $this->assertNotExpired($version);
                if ($version->status !== ContractVersion::STATUS_PENDING) {
                    throw ApiException::state("version is {$version->status}; only a pending version can be signed");
                }

                $signers = $version->signerList();
                $position = array_search($signer, $signers, true);
                if ($position === false) {
                    throw new ApiException("signer '{$signer}' is not a designated signer of this version", 422);
                }

                // Strict order: every earlier position must already be signed.
                $priorCount = Signature::query()
                    ->where('contract_version_id', $version->id)
                    ->where('position', '<', $position)
                    ->count();
                if ($priorCount < $position) {
                    throw ApiException::state(
                        'signing order: earlier signers must confirm first',
                        ['next_expected_position' => $priorCount]
                    );
                }

                $now = Clock::now();
                $nowStr = $now->format('Y-m-d H:i:s.u');
                $sig = new Signature();
                $sig->fill([
                    'contract_version_id' => $version->id,
                    'signer' => $signer,
                    'position' => $position,
                    'content_hash' => $expectedHash,
                    'content_version' => $version->version_no,
                    'status' => 'signed',
                    'signed_by' => $actor,
                    'signed_at' => $nowStr,
                    'created_at' => $nowStr,
                    'updated_at' => $nowStr,
                ]);
                try {
                    $sig->save();
                } catch (\Illuminate\Database\QueryException $e) {
                    // Duplicate key from a concurrent first confirmation.
                    if ($e->getCode() === '23000') {
                        throw ApiException::conflict('signer already confirmed (concurrent request)', 'duplicate_signature');
                    }
                    throw $e;
                }

                // All positions signed -> version completed -> contract active.
                $total = count($signers);
                $done = Signature::query()->where('contract_version_id', $version->id)->count();
                if ($done >= $total) {
                    $this->completeVersion($version, $nowStr);
                }

                return $sig;
            }
        );

        return ['signature' => $signature, 'idempotent' => $idempotent];
    }

    /** Withdrawal is only legal before the first signature and within 72h. */
    public function withdraw(int $versionId, string $actor): ContractVersion
    {
        return \TravelOps\Locks::withTransactionalLock('sign', (string) $versionId, function () use ($versionId, $actor): ContractVersion {
            $version = $this->lockedVersion($versionId);
            $this->assertNotExpired($version);

            if ($version->status !== ContractVersion::STATUS_PENDING) {
                throw ApiException::state("version is {$version->status}; only a pending version can be withdrawn");
            }
            if (Signature::query()->where('contract_version_id', $version->id)->exists()) {
                throw ApiException::state('cannot withdraw after the first signature');
            }

            $nowStr = Clock::now()->format('Y-m-d H:i:s.u');
            $version->fill(['status' => ContractVersion::STATUS_WITHDRAWN, 'withdrawn_at' => $nowStr, 'updated_at' => $nowStr]);
            $version->save();

            $contract = Contract::query()->find($version->contract_id);
            $contract->fill(['status' => Contract::STATUS_WITHDRAWN, 'updated_at' => $nowStr]);
            $contract->save();

            return $version;
        });
    }

    /**
     * Mark a still-pending version expired once its 72h window has passed.
     * Returns null if the version was already in a terminal state.
     */
    public function expireIfDue(int $versionId): ?ContractVersion
    {
        return \TravelOps\Locks::withTransactionalLock('sign', (string) $versionId, function () use ($versionId): ?ContractVersion {
            $version = $this->lockedVersion($versionId);
            if ($version->status !== ContractVersion::STATUS_PENDING) {
                return $version;
            }
            if (Clock::now() < new \DateTimeImmutable($version->expires_at)) {
                return null;
            }

            $nowStr = Clock::now()->format('Y-m-d H:i:s.u');
            $version->fill(['status' => ContractVersion::STATUS_EXPIRED, 'expired_at' => $nowStr, 'updated_at' => $nowStr]);
            $version->save();

            $contract = Contract::query()->find($version->contract_id);
            if ((int) $contract->current_version_id === (int) $version->id) {
                $contract->fill(['status' => Contract::STATUS_EXPIRED, 'updated_at' => $nowStr]);
                $contract->save();
            }
            return $version;
        });
    }

    /** Mark every due pending version expired (maintenance command). */
    public function sweepExpired(): int
    {
        $ids = ContractVersion::query()
            ->where('status', ContractVersion::STATUS_PENDING)
            ->where('expires_at', '<=', Clock::now()->format('Y-m-d H:i:s.u'))
            ->pluck('id');
        $count = 0;
        foreach ($ids as $id) {
            if ($this->expireIfDue((int) $id) !== null) {
                $count++;
            }
        }
        return $count;
    }

    private function lockedVersion(int $versionId): ContractVersion
    {
        // Lock the parent contract row first: sign, withdraw and expiry all
        // contend on the same row, so only one valid-state transition runs.
        $version = ContractVersion::query()->find($versionId);
        if (!$version) {
            throw ApiException::notFound('contract version');
        }
        Contract::query()->lockForUpdate()->find($version->contract_id);
        return ContractVersion::query()->lockForUpdate()->find($version->id);
    }

    private function assertNotExpired(ContractVersion $version): void
    {
        // The 72h boundary is enforced here regardless of whether the row has
        // been marked expired yet (the sweep command / expire endpoint performs
        // that marking). We must not perform that update inside this caller's
        // transaction: the caller is about to throw and would roll it back.
        if (Clock::now() >= new \DateTimeImmutable($version->expires_at)) {
            throw ApiException::state('the 72-hour signing window has elapsed; version can no longer be signed', [
                'expires_at' => $version->expires_at,
            ]);
        }
    }

    private function completeVersion(ContractVersion $version, string $nowStr): void
    {
        // Any previously active version is superseded — its signatures remain
        // historically bound to it and never carry over to the new one.
        ContractVersion::query()
            ->where('contract_id', $version->contract_id)
            ->where('status', ContractVersion::STATUS_SIGNED)
            ->where('id', '!=', $version->id)
            ->update(['status' => 'superseded', 'updated_at' => $nowStr]);

        $version->fill(['status' => ContractVersion::STATUS_SIGNED, 'completed_at' => $nowStr, 'updated_at' => $nowStr]);
        $version->save();

        $contract = Contract::query()->find($version->contract_id);
        $contract->fill(['status' => Contract::STATUS_ACTIVE, 'current_version_id' => $version->id, 'updated_at' => $nowStr]);
        $contract->save();
    }
}
