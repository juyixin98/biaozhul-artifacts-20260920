<?php

declare(strict_types=1);

namespace TravelOpsTests;

use TravelOps\Canonical;
use TravelOps\Clock;
use TravelOps\Models\Contract;
use TravelOps\Models\ContractTemplate;
use TravelOps\Models\ContractVersion;
use TravelOps\Models\Signature;
use TravelOps\Services\ContractService;
use TravelOps\Services\RuleService;

/** Tiny scenario builder shared by the tests. */
trait BuildsContracts
{
    protected ?ContractTemplate $template = null;

    protected function makeTemplate(string $code = 'TPL-1', string $body = 'A={{a}} B={{b}}'): ContractTemplate
    {
        $now = Clock::now()->format('Y-m-d H:i:s.u');
        $t = new ContractTemplate();
        $t->fill([
            'code' => $code,
            'name' => 'Template ' . $code,
            'body_template' => $body,
            'version' => 1,
            'created_by' => 'tester',
            'created_at' => $now,
            'updated_at' => $now,
        ]);
        $t->save();
        return $t;
    }

    protected function makeContract(string $code = 'C-1', string $customer = 'Customer'): Contract
    {
        return (new ContractService())->createContract(
            ['code' => $code, 'customer_name' => $customer],
            'tester'
        );
    }

    protected function initiate(
        Contract $c,
        array $variables = ['a' => '1', 'b' => '2'],
        array $signers = ['alice', 'bob'],
        ?ContractTemplate $t = null
    ): ContractVersion {
        return (new ContractService())->initiateVersion((int) $c->id, [
            'template_code' => ($t ?? $this->template ?? null)?->code,
            'variables' => $variables,
            'signers' => $signers,
        ], 'tester');
    }

    protected function fullySign(ContractVersion $v): void
    {
        $svc = new \TravelOps\Services\SigningService();
        foreach ($v->signerList() as $signer) {
            $svc->sign((int) $v->id, $signer, $v->content_hash, $signer);
        }
    }

    protected function makeRules(Contract $c, array $rules): int
    {
        return (int) (new RuleService())->createVersion((int) $c->id, $rules, 'tester')->id;
    }
}
