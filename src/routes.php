<?php

declare(strict_types=1);

use Illuminate\Database\Capsule\Manager as Capsule;
use Psr\Http\Message\ResponseInterface as Response;
use Psr\Http\Message\ServerRequestInterface as Request;
use Slim\App;
use TravelOps\ApiException;
use TravelOps\Models\Contract;
use TravelOps\Models\ContractTemplate;
use TravelOps\Models\ContractVersion;
use TravelOps\Models\Settlement;
use TravelOps\Services\CloseService;
use TravelOps\Services\ContractService;
use TravelOps\Services\CorrectionService;
use TravelOps\Services\ImportService;
use TravelOps\Services\RuleService;
use TravelOps\Services\SettlementService;
use TravelOps\Services\SigningService;

/**
 * @param Closure(Response,mixed,int=):Response $json
 * @param Closure(Request):string $actor
 */
return function (App $app, Closure $json, Closure $actor): void {
    $body = fn (Request $r): array => (array) ($r->getParsedBody() ?? []);

    $app->get('/health', function (Request $r, Response $res) use ($json) {
        $db = 'ok';
        try {
            Capsule::connection()->select('SELECT 1');
        } catch (Throwable $e) {
            $db = 'down';
        }
        return $json($res, ['status' => $db === 'ok' ? 'ok' : 'degraded', 'database' => $db], $db === 'ok' ? 200 : 503);
    });

    // ---- templates ---------------------------------------------------------
    $app->post('/api/templates', function (Request $r, Response $res) use ($json, $actor, $body) {
        $t = (new ContractService())->createTemplate($body($r), $actor($r));
        return $json($res, ['id' => (int) $t->id, 'code' => $t->code], 201);
    });
    $app->get('/api/templates', function (Request $r, Response $res) use ($json) {
        $rows = ContractTemplate::query()->orderBy('id')->get()->map(static fn ($t) => [
            'id' => (int) $t->id, 'code' => $t->code, 'name' => $t->name, 'version' => (int) $t->version,
        ]);
        return $json($res, ['templates' => $rows]);
    });

    // ---- contracts & versions ---------------------------------------------
    $app->post('/api/contracts', function (Request $r, Response $res) use ($json, $actor, $body) {
        $c = (new ContractService())->createContract($body($r), $actor($r));
        return $json($res, ['id' => (int) $c->id, 'code' => $c->code, 'status' => $c->status], 201);
    });
    $app->get('/api/contracts', function (Request $r, Response $res) use ($json) {
        return $json($res, ['contracts' => Contract::query()->orderBy('id')->get()]);
    });
    $app->get('/api/contracts/{id}', function (Request $r, Response $res, array $a) use ($json) {
        $c = Contract::query()->with('versions')->find((int) $a['id']) ?? throw ApiException::notFound('contract');
        return $json($res, $c);
    });

    $app->post('/api/contracts/{id}/versions', function (Request $r, Response $res, array $a) use ($json, $actor, $body) {
        $v = (new ContractService())->initiateVersion((int) $a['id'], $body($r), $actor($r));
        return $json($res, [
            'id' => (int) $v->id,
            'version_no' => (int) $v->version_no,
            'status' => $v->status,
            'content_hash' => $v->content_hash,
            'summary' => $v->summary(),
            'expires_at' => $v->expires_at,
            'frozen_content' => $v->frozen_content,
        ], 201);
    });

    $app->get('/api/contracts/{id}/versions', function (Request $r, Response $res, array $a) use ($json) {
        $rows = ContractVersion::query()->where('contract_id', (int) $a['id'])->orderBy('version_no')->get();
        return $json($res, ['versions' => $rows]);
    });

    // ---- signing / withdrawal / expiry ------------------------------------
    $app->post('/api/versions/{id}/sign', function (Request $r, Response $res, array $a) use ($json, $actor, $body) {
        $data = $body($r);
        $signer = (string) ($data['signer'] ?? '');
        $hash = (string) ($data['content_hash'] ?? '');
        if ($signer === '') {
            throw new ApiException('signer is required', 422);
        }
        $result = (new SigningService())->sign((int) $a['id'], $signer, $hash, $actor($r));
        return $json($res, [
            'signature_id' => (int) $result['signature']->id,
            'signer' => $result['signature']->signer,
            'position' => (int) $result['signature']->position,
            'content_version' => (int) $result['signature']->content_version,
            'signed_at' => $result['signature']->signed_at,
            'idempotent' => $result['idempotent'],
        ]);
    });

    $app->post('/api/versions/{id}/withdraw', function (Request $r, Response $res, array $a) use ($json, $actor) {
        $v = (new SigningService())->withdraw((int) $a['id'], $actor($r));
        return $json($res, ['id' => (int) $v->id, 'status' => $v->status, 'withdrawn_at' => $v->withdrawn_at]);
    });

    $app->post('/api/versions/{id}/expire', function (Request $r, Response $res, array $a) use ($json) {
        $v = (new SigningService())->expireIfDue((int) $a['id']);
        if ($v === null) {
            throw ApiException::conflict('version is still within its 72-hour window', 'not_due');
        }
        return $json($res, ['id' => (int) $v->id, 'status' => $v->status]);
    });

    $app->post('/api/maintenance/expire-due', function (Request $r, Response $res) use ($json) {
        $n = (new SigningService())->sweepExpired();
        return $json($res, ['expired_versions' => $n]);
    });

    // ---- allocation rules --------------------------------------------------
    $app->post('/api/contracts/{id}/rule-versions', function (Request $r, Response $res, array $a) use ($json, $actor, $body) {
        $data = $body($r);
        $rules = $data['rules'] ?? [];
        $rv = (new RuleService())->createVersion((int) $a['id'], (array) $rules, $actor($r));
        return $json($res, ['id' => (int) $rv->id, 'rule_version' => (int) $rv->rule_version, 'status' => $rv->status], 201);
    });
    $app->get('/api/contracts/{id}/rule-versions', function (Request $r, Response $res, array $a) use ($json) {
        $active = (new RuleService())->activeVersion((int) $a['id']);
        return $json($res, ['active' => $active ? ['id' => (int) $active->id, 'rule_version' => (int) $active->rule_version, 'rules' => $active->rules()] : null]);
    });

    // ---- import & aggregation ---------------------------------------------
    $app->post('/api/imports', function (Request $r, Response $res) use ($json, $actor, $body) {
        $data = $body($r);
        $entries = $data['entries'] ?? [];
        if (!is_array($entries)) {
            throw new ApiException('entries must be an array', 422);
        }
        $result = (new ImportService())->importBatch($entries, $actor($r), isset($data['batch_ref']) ? (string) $data['batch_ref'] : null);
        return $json($res, $result, 201);
    });
    $app->get('/api/aggregations', function (Request $r, Response $res) use ($json) {
        $contractId = $r->getQueryParams()['contract_id'] ?? null;
        $rows = (new ImportService())->aggregation($contractId !== null ? (int) $contractId : null);
        return $json($res, ['groups' => $rows]);
    });
    $app->get('/api/entries', function (Request $r, Response $res) use ($json) {
        $q = $r->getQueryParams();
        $query = \TravelOps\Models\SettlementEntry::query()->orderBy('id');
        foreach (['contract_id' => 'contract_id', 'currency' => 'currency', 'business_date' => 'business_date'] as $param => $col) {
            if (isset($q[$param])) {
                $query->where($col, $q[$param]);
            }
        }
        return $json($res, ['entries' => $query->limit(500)->get()]);
    });

    // ---- settlements -------------------------------------------------------
    $app->post('/api/settlements', function (Request $r, Response $res) use ($json, $actor, $body) {
        $data = $body($r);
        $result = (new SettlementService())->create([
            'contract_id' => (int) ($data['contract_id'] ?? 0),
            'currency' => (string) ($data['currency'] ?? ''),
            'business_date' => (string) ($data['business_date'] ?? ''),
            'idempotency_key' => isset($data['idempotency_key']) ? (string) $data['idempotency_key'] : null,
        ], $actor($r));
        return $json($res, $result, $result['idempotent'] ? 200 : 201);
    });
    $app->get('/api/settlements', function (Request $r, Response $res) use ($json) {
        $rows = Settlement::query()->orderBy('id')->with('allocations')->get()
            ->map(fn (Settlement $s) => (new SettlementService())->present($s));
        return $json($res, ['settlements' => $rows]);
    });

    // ---- corrections (closed-day reversal + adjustment) -------------------
    $app->post('/api/entries/{id}/reverse', function (Request $r, Response $res, array $a) use ($json, $actor, $body) {
        $data = $body($r);
        $postingDate = (string) ($data['posting_date'] ?? '');
        $result = (new CorrectionService())->reverse(
            (int) $a['id'],
            $postingDate,
            $actor($r),
            isset($data['adjustment']) ? (string) $data['adjustment'] : null
        );
        return $json($res, $result, 201);
    });

    // ---- daily close -------------------------------------------------------
    $app->post('/api/closes', function (Request $r, Response $res) use ($json, $actor, $body) {
        $data = $body($r);
        $date = (string) ($data['business_date'] ?? '');
        $result = (new CloseService())->close($date, $actor($r));
        return $json($res, $result, $result['idempotent'] ? 200 : 201);
    });
    $app->get('/api/closes', function (Request $r, Response $res) use ($json) {
        return $json($res, ['closes' => (new CloseService())->listCloses()]);
    });
};
