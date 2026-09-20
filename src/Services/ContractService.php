<?php

declare(strict_types=1);

namespace TravelOps\Services;

use TravelOps\ApiException;
use TravelOps\Canonical;
use TravelOps\Clock;
use TravelOps\Models\Contract;
use TravelOps\Models\ContractTemplate;
use TravelOps\Models\ContractVersion;

/**
 * Contract lifecycle up to (but not including) a signature:
 * template creation, contract drafts, and version initiation.
 *
 * When signing is initiated the *complete* rendered content plus a summary are
 * frozen into the version row, together with a sha256 content hash. Every
 * later signature is bound to that exact hash.
 */
final class ContractService
{
    /** @param array<string,mixed> $data */
    public function createTemplate(array $data, string $actor): ContractTemplate
    {
        $code = (string) ($data['code'] ?? '');
        $name = (string) ($data['name'] ?? '');
        $body = (string) ($data['body_template'] ?? '');
        if ($code === '' || $name === '' || $body === '') {
            throw new ApiException('code, name and body_template are required', 422);
        }
        if (ContractTemplate::query()->where('code', $code)->exists()) {
            throw ApiException::conflict("template code {$code} already exists");
        }

        $now = Clock::now()->format('Y-m-d H:i:s.u');
        $template = new ContractTemplate();
        $template->fill([
            'code' => $code,
            'name' => $name,
            'body_template' => $body,
            'version' => 1,
            'created_by' => $actor,
            'created_at' => $now,
            'updated_at' => $now,
        ]);
        $template->save();
        return $template;
    }

    /** @param array<string,mixed> $data */
    public function createContract(array $data, string $actor): Contract
    {
        $code = (string) ($data['code'] ?? '');
        $customer = (string) ($data['customer_name'] ?? '');
        if ($code === '' || $customer === '') {
            throw new ApiException('code and customer_name are required', 422);
        }
        if (Contract::query()->where('code', $code)->exists()) {
            throw ApiException::conflict("contract code {$code} already exists");
        }

        $now = Clock::now()->format('Y-m-d H:i:s.u');
        $contract = new Contract();
        $contract->fill([
            'code' => $code,
            'customer_name' => $customer,
            'status' => Contract::STATUS_DRAFT,
            'created_by' => $actor,
            'created_at' => $now,
            'updated_at' => $now,
        ]);
        $contract->save();
        return $contract;
    }

    /**
     * Render the template, freezing content + summary + hash into a new
     * pending version. Content changes always create a NEW version; prior
     * confirmations never carry over (each version has its own signatures).
     *
     * @param array<string,mixed> $data
     */
    public function initiateVersion(int $contractId, array $data, string $actor): ContractVersion
    {
        $contract = Contract::query()->find($contractId);
        if (!$contract) {
            throw ApiException::notFound('contract');
        }

        $templateCode = $data['template_code'] ?? null;
        $template = $templateCode
            ? ContractTemplate::query()->where('code', $templateCode)->first()
            : ContractTemplate::query()->orderBy('id')->first();
        if (!$template) {
            throw new ApiException('no template available', 422);
        }

        $variables = $data['variables'] ?? [];
        if (!is_array($variables)) {
            throw new ApiException('variables must be an object', 422);
        }
        $signers = $data['signers'] ?? [];
        if (!is_array($signers) || !array_is_list($signers) || count($signers) < 1) {
            throw new ApiException('signers must be a non-empty ordered array', 422);
        }
        $signers = array_map('strval', $signers);
        if (count(array_unique($signers)) !== count($signers)) {
            throw new ApiException('signers must be unique', 422);
        }

        $rendered = $this->render($template, $variables);

        return \TravelOps\Locks::withTransactionalLock('contract', (string) $contract->id, function () use (
            $contract, $template, $variables, $signers, $rendered, $actor
        ): ContractVersion {
            /** @var Contract $contract */
            $contract = Contract::query()->lockForUpdate()->find($contract->id);

            // If a pending version already exists it must be withdrawn or left
            // to expire before a new one is initiated.
            $openPending = ContractVersion::query()
                ->where('contract_id', $contract->id)
                ->where('status', ContractVersion::STATUS_PENDING)
                ->exists();
            if ($openPending) {
                throw ApiException::state(
                    'a pending version exists; withdraw it or wait for expiry before initiating a new version'
                );
            }

            $versionNo = ((int) ContractVersion::query()
                ->where('contract_id', $contract->id)->max('version_no')) + 1;

            $now = Clock::now();
            $nowStr = $now->format('Y-m-d H:i:s.u');
            $expires = $now->modify('+72 hours');

            $summary = $this->buildSummary($contract, $template, $variables, $rendered, $signers);
            $contentHash = hash('sha256', $rendered);

            $version = new ContractVersion();
            $version->fill([
                'contract_id' => $contract->id,
                'version_no' => $versionNo,
                'template_id' => $template->id,
                'variables_json' => Canonical::json($variables),
                'frozen_content' => $rendered,
                'content_hash' => $contentHash,
                'summary_json' => Canonical::json($summary),
                'signer_order' => json_encode(array_values($signers), JSON_UNESCAPED_UNICODE),
                'status' => ContractVersion::STATUS_PENDING,
                'expires_at' => $expires->format('Y-m-d H:i:s.u'),
                'initiated_by' => $actor,
                'initiated_at' => $nowStr,
                'created_at' => $nowStr,
                'updated_at' => $nowStr,
            ]);
            $version->save();

            $contract->fill([
                'status' => Contract::STATUS_PENDING,
                'current_version_id' => $version->id,
                'updated_at' => $nowStr,
            ]);
            $contract->save();

            return $version;
        });
    }

    /**
     * Render a template with variables. Every {{placeholder}} must be
     * supplied; unknown variables are rejected (fail closed, no silent blanks).
     *
     * @param array<string,mixed> $variables
     */
    public function render(ContractTemplate $template, array $variables): string
    {
        $required = $template->decodeVariablesPlaceholders();
        $missing = array_values(array_diff($required, array_keys($variables)));
        if ($missing) {
            throw new ApiException(
                'missing template variables: ' . implode(', ', $missing),
                422,
                'missing_variables',
                ['missing' => $missing]
            );
        }
        $unknown = array_values(array_diff(array_keys($variables), $required));
        if ($unknown) {
            throw new ApiException(
                'unknown variables supplied: ' . implode(', ', $unknown),
                422,
                'unknown_variables',
                ['unknown' => $unknown]
            );
        }

        $rendered = preg_replace_callback(
            '/\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}/',
            static fn (array $m): string => self::stringify($variables[$m[1]]),
            $template->body_template
        );
        return $rendered;
    }

    /**
     * @param array<string,mixed> $variables
     * @return array<string,mixed>
     */
    private function buildSummary(
        Contract $contract,
        ContractTemplate $template,
        array $variables,
        string $rendered,
        array $signers
    ): array {
        return [
            'contract_code' => $contract->code,
            'customer_name' => $contract->customer_name,
            'template' => ['code' => $template->code, 'name' => $template->name],
            'variables' => $variables,
            'signer_order' => array_values($signers),
            'content_length' => mb_strlen($rendered),
            'preview' => mb_substr($rendered, 0, 200),
        ];
    }

    private static function stringify(mixed $v): string
    {
        if (is_scalar($v) || $v === null) {
            return (string) ($v ?? '');
        }
        throw new ApiException('variable values must be scalar', 422);
    }
}
