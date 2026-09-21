<?php

declare(strict_types=1);

namespace Meridian\Services;

use Illuminate\Database\Capsule\Manager as DB;
use Meridian\DomainException;
use Meridian\Models\Contract;
use Meridian\Models\ContractEvent;
use Meridian\Models\ContractSigner;
use Meridian\Models\ContractTemplate;
use Meridian\Models\ContractVersion;
use Meridian\Support\Clock;

/**
 * Contract lifecycle: template rendering, versioned content, ordered signing.
 *
 * Invariants enforced here:
 *  - Content is frozen (full text + sha256 digest) when signing is initiated.
 *  - Signers confirm strictly in seq order; every confirmation is bound to the
 *    exact version row it was made against.
 *  - Any content change creates a new version and supersedes the previous
 *    draft/signing version; confirmations never carry over.
 *  - Withdraw is only possible before the first confirmation.
 *  - A signing version expires 72h after initiation; expiry is applied lazily
 *    on any state transition attempt (and via the expire endpoint).
 *  - All transitions serialise on the version row (SELECT ... FOR UPDATE) plus
 *    atomic guarded UPDATEs, so concurrent confirm/withdraw/expire requests
 *    resolve to exactly one valid outcome and retries stay idempotent.
 *
 * NOTE: confirmations record operator and timestamp for operational audit
 * only; this system makes no legal/non-repudiation certification claims.
 */
final class ContractService
{
    public const SIGN_TTL_HOURS = 72;

    public function createTemplate(string $name, string $body): ContractTemplate
    {
        return ContractTemplate::create(['name' => $name, 'body' => $body]);
    }

    public function createContract(string $code, int $templateId, string $title, array $variables, string $operator): Contract
    {
        $template = ContractTemplate::find($templateId)
            ?? throw new DomainException("template {$templateId} not found", 404, 'template_not_found');

        $content = $this->render($template->body, $variables);

        return DB::transaction(function () use ($code, $template, $title, $variables, $operator, $content) {
            $contract = Contract::create([
                'code' => $code,
                'template_id' => $template->id,
                'title' => $title,
                'status' => 'draft',
                'created_by' => $operator,
            ]);
            $this->newVersionRow($contract, 1, $variables, $content);
            $this->event($contract->id, null, 'contract_created', $operator);

            return $contract->fresh(['versions']);
        });
    }

    /**
     * Content change => new version. The previous draft/signing version is
     * superseded and its pending confirmations become void (they were bound
     * to the old content hash and must not be reused).
     */
    public function createVersion(int $contractId, array $variables, string $operator): ContractVersion
    {
        return DB::transaction(function () use ($contractId, $variables, $operator) {
            /** @var Contract $contract */
            $contract = Contract::where('id', $contractId)->lockForUpdate()->first()
                ?? throw new DomainException("contract {$contractId} not found", 404, 'contract_not_found');

            if (!in_array($contract->status, ['draft', 'signing'], true)) {
                throw new DomainException(
                    "contract is '{$contract->status}'; content changes are only possible before signing completes",
                    409,
                    'contract_locked'
                );
            }

            $current = $contract->versions()
                ->whereIn('status', ['draft', 'signing'])
                ->orderByDesc('version_no')
                ->first();
            if ($current !== null) {
                $current->status = 'superseded';
                $current->save();
                $this->event($contract->id, $current->id, 'version_superseded', $operator);
            }

            $content = $this->render($contract->template->body, $variables);
            $nextNo = (int) $contract->versions()->max('version_no') + 1;
            $version = $this->newVersionRow($contract, $nextNo, $variables, $content);

            $contract->status = 'draft';
            $contract->save();
            $this->event($contract->id, $version->id, 'version_created', $operator, ['version_no' => $nextNo]);

            return $version;
        });
    }

    /**
     * Freeze content + digest and open the ordered signing window (72h).
     */
    public function initiateSign(int $contractId, int $versionNo, array $signers, string $operator): ContractVersion
    {
        if (count($signers) === 0) {
            throw new DomainException('at least one signer is required', 422, 'no_signers');
        }

        return DB::transaction(function () use ($contractId, $versionNo, $signers, $operator) {
            $contract = Contract::where('id', $contractId)->lockForUpdate()->first()
                ?? throw new DomainException("contract {$contractId} not found", 404, 'contract_not_found');
            $version = $this->lockedVersion($contractId, $versionNo);

            if ($version->status !== 'draft' || $contract->status !== 'draft') {
                throw new DomainException(
                    "version {$versionNo} is '{$version->status}', cannot initiate signing",
                    409,
                    'invalid_state'
                );
            }

            $now = Clock::now();
            // Atomic guard: only one initiation can win even without the row lock.
            $affected = DB::update(
                'UPDATE contract_versions SET status = ?, sign_initiated_at = ?, expires_at = ?, updated_at = ? '
                . 'WHERE id = ? AND status = ?',
                ['signing', $now->format('Y-m-d H:i:s'), $now->modify('+' . self::SIGN_TTL_HOURS . ' hours')->format('Y-m-d H:i:s'),
                 $now->format('Y-m-d H:i:s'), $version->id, 'draft']
            );
            if ($affected !== 1) {
                throw new DomainException('signing already initiated for this version', 409, 'invalid_state');
            }

            $seq = 1;
            foreach ($signers as $s) {
                ContractSigner::create([
                    'version_id' => $version->id,
                    'seq' => $seq++,
                    'signer_name' => $s['name'] ?? throw new DomainException('signer name required', 422, 'invalid_signer'),
                    'signer_role' => $s['role'] ?? null,
                    'status' => 'pending',
                ]);
            }

            $contract->status = 'signing';
            $contract->save();
            $this->event($contract->id, $version->id, 'sign_initiated', $operator, [
                'content_hash' => $version->content_hash,
                'expires_at' => $version->fresh()->expires_at,
            ]);

            return $version->fresh(['signers']);
        });
    }

    /**
     * Confirm one signer, in order, against a specific version.
     * Idempotent: re-confirming an already-confirmed signer returns the
     * existing confirmation instead of failing or duplicating.
     */
    public function confirm(int $contractId, int $versionNo, int $seq, string $operator): array
    {
        $result = DB::transaction(function () use ($contractId, $versionNo, $seq, $operator) {
            $version = $this->lockedVersion($contractId, $versionNo);
            if ($this->applyExpiry($version, $operator)) {
                // Expiry transition commits with this transaction; the 409 is
                // thrown after commit so the expired state is not rolled back.
                return ['expired' => true];
            }

            /** @var ContractSigner|null $signer */
            $signer = ContractSigner::where('version_id', $version->id)->where('seq', $seq)->first()
                ?? throw new DomainException("signer seq {$seq} not found on version {$versionNo}", 404, 'signer_not_found');

            if ($signer->status === 'confirmed') {
                // Retry of an already-applied confirmation: safe no-op.
                return ['version' => $version->fresh(['signers']), 'signer' => $signer, 'idempotent' => true];
            }

            if ($version->status !== 'signing') {
                throw new DomainException(
                    "version {$versionNo} is '{$version->status}', confirmations are not accepted",
                    409,
                    'invalid_state'
                );
            }

            $earlierPending = ContractSigner::where('version_id', $version->id)
                ->where('seq', '<', $seq)
                ->where('status', 'pending')
                ->count();
            if ($earlierPending > 0) {
                throw new DomainException('signers must confirm in order', 409, 'out_of_order');
            }

            $now = Clock::now();
            $affected = DB::update(
                'UPDATE contract_signers SET status = ?, confirmed_at = ?, confirmed_by = ?, updated_at = ? '
                . 'WHERE id = ? AND status = ?',
                ['confirmed', $now->format('Y-m-d H:i:s'), $operator, $now->format('Y-m-d H:i:s'), $signer->id, 'pending']
            );
            if ($affected !== 1) {
                // Lost a race; re-read and report the effective outcome.
                $signer->refresh();
                if ($signer->status === 'confirmed') {
                    return ['version' => $version->fresh(['signers']), 'signer' => $signer, 'idempotent' => true];
                }
                throw new DomainException('confirmation could not be applied', 409, 'invalid_state');
            }

            $this->event($contractId, $version->id, 'confirmed', $operator, ['seq' => $seq]);

            $remaining = ContractSigner::where('version_id', $version->id)->where('status', 'pending')->count();
            if ($remaining === 0) {
                $version->status = 'signed';
                $version->save();
                $contract = Contract::find($contractId);
                $contract->status = 'signed';
                $contract->save();
                $this->event($contractId, $version->id, 'signed', $operator);
            }

            return [
                'version' => $version->fresh(['signers']),
                'signer' => $signer->fresh(),
                'idempotent' => false,
            ];
        });

        if ($result['expired'] ?? false) {
            throw new DomainException('signing window has expired', 409, 'expired');
        }

        return $result;
    }

    /**
     * Withdraw is allowed only before the first confirmation.
     */
    public function withdraw(int $contractId, string $operator): Contract
    {
        $expired = false;
        $contract = DB::transaction(function () use ($contractId, $operator, &$expired) {
            /** @var Contract $contract */
            $contract = Contract::where('id', $contractId)->lockForUpdate()->first()
                ?? throw new DomainException("contract {$contractId} not found", 404, 'contract_not_found');

            /** @var ContractVersion|null $version */
            $version = $contract->versions()->where('status', 'signing')->lockForUpdate()->first();
            if ($version === null) {
                throw new DomainException('no signing-in-progress version to withdraw', 409, 'invalid_state');
            }
            if ($this->applyExpiry($version, $operator)) {
                $expired = true;
                return $contract;
            }

            $confirmed = ContractSigner::where('version_id', $version->id)->where('status', 'confirmed')->count();
            if ($confirmed > 0) {
                throw new DomainException('cannot withdraw after the first confirmation', 409, 'already_signed');
            }

            // Guarded update: if a confirmation landed between check and write,
            // zero rows are affected and we report the conflict.
            $affected = DB::update(
                'UPDATE contract_versions SET status = ?, updated_at = ? WHERE id = ? AND status = ?',
                ['withdrawn', Clock::now()->format('Y-m-d H:i:s'), $version->id, 'signing']
            );
            if ($affected !== 1) {
                throw new DomainException('version state changed concurrently; withdrawal rejected', 409, 'invalid_state');
            }

            $contract->status = 'withdrawn';
            $contract->save();
            $this->event($contractId, $version->id, 'withdrawn', $operator);

            return $contract->fresh();
        });

        if ($expired) {
            throw new DomainException('signing window has expired', 409, 'expired');
        }

        return $contract;
    }

    /**
     * Apply expiry to the active signing version if its 72h window has closed.
     * Returns the (possibly unchanged) contract.
     */
    public function expire(int $contractId, string $operator): Contract
    {
        return DB::transaction(function () use ($contractId, $operator) {
            $contract = Contract::where('id', $contractId)->lockForUpdate()->first()
                ?? throw new DomainException("contract {$contractId} not found", 404, 'contract_not_found');
            $version = $contract->versions()->where('status', 'signing')->lockForUpdate()->first();
            if ($version !== null) {
                $this->applyExpiry($version, $operator);
            }
            return $contract->fresh();
        });
    }

    public function getContract(int $contractId): Contract
    {
        return Contract::with(['versions.signers', 'events'])->find($contractId)
            ?? throw new DomainException("contract {$contractId} not found", 404, 'contract_not_found');
    }

    private function lockedVersion(int $contractId, int $versionNo): ContractVersion
    {
        return ContractVersion::where('contract_id', $contractId)
            ->where('version_no', $versionNo)
            ->lockForUpdate()
            ->first()
            ?? throw new DomainException("version {$versionNo} not found on contract {$contractId}", 404, 'version_not_found');
    }

    /**
     * Lazily transition a signing version past its deadline to 'expired'.
     * Never throws: callers commit the transition and then decide how to
     * report it, so the expired state is not rolled back with the action.
     *
     * @return bool true if the version is expired (already or just now)
     */
    private function applyExpiry(ContractVersion $version, string $operator): bool
    {
        if ($version->status === 'expired') {
            return true;
        }
        if ($version->status !== 'signing' || $version->expires_at === null) {
            return false;
        }
        if (Clock::now() < $version->expires_at->toDateTimeImmutable()) {
            return false;
        }

        $version->status = 'expired';
        $version->save();
        $contract = Contract::find($version->contract_id);
        if ($contract !== null && $contract->status === 'signing') {
            $contract->status = 'expired';
            $contract->save();
        }
        $this->event($version->contract_id, $version->id, 'expired', $operator);

        return true;
    }

    private function newVersionRow(Contract $contract, int $versionNo, array $variables, string $content): ContractVersion
    {
        return ContractVersion::create([
            'contract_id' => $contract->id,
            'version_no' => $versionNo,
            'variables' => $variables,
            'content' => $content,
            'content_hash' => hash('sha256', $content),
            'status' => 'draft',
        ]);
    }

    private function render(string $body, array $variables): string
    {
        $missing = [];
        $out = preg_replace_callback(
            '/\{\{\s*([A-Za-z0-9_]+)\s*\}\}/',
            function (array $m) use ($variables, &$missing) {
                if (!array_key_exists($m[1], $variables)) {
                    $missing[] = $m[1];
                    return $m[0];
                }
                return (string) $variables[$m[1]];
            },
            $body
        );
        if ($missing !== []) {
            throw new DomainException(
                'missing template variables: ' . implode(', ', array_unique($missing)),
                422,
                'missing_variables'
            );
        }
        return (string) $out;
    }

    private function event(int $contractId, ?int $versionId, string $action, string $operator, array $detail = []): void
    {
        ContractEvent::create([
            'contract_id' => $contractId,
            'version_id' => $versionId,
            'action' => $action,
            'operator' => $operator,
            'detail' => $detail === [] ? null : $detail,
            'created_at' => Clock::now()->format('Y-m-d H:i:s'),
        ]);
    }
}
