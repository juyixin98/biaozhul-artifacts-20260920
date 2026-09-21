<?php

declare(strict_types=1);

use Meridian\DomainException;
use Meridian\Services\CloseService;
use Meridian\Services\ContractService;
use Meridian\Services\ImportService;
use Meridian\Services\SettlementService;
use Meridian\Support\Database;
use Psr\Http\Message\ResponseInterface as Response;
use Psr\Http\Message\ServerRequestInterface as Request;
use Slim\Exception\HttpException;
use Slim\Factory\AppFactory;

require __DIR__ . '/../vendor/autoload.php';

Database::init();

$app = AppFactory::create();
$app->addBodyParsingMiddleware();

$errorMiddleware = $app->addErrorMiddleware(false, true, true);
$errorMiddleware->setDefaultErrorHandler(
    function (Request $request, Throwable $e) use ($app): Response {
        if ($e instanceof DomainException) {
            $status = $e->status;
            $code = $e->errorCode;
        } elseif ($e instanceof HttpException) {
            $status = $e->getCode();
            $code = 'http_error';
        } else {
            $status = 500;
            $code = 'internal_error';
        }
        $response = $app->getResponseFactory()->createResponse();
        return json($response, ['error' => ['code' => $code, 'message' => $e->getMessage()]], $status);
    }
);

function json(Response $response, mixed $data, int $status = 200): Response
{
    $response->getBody()->write(json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
    return $response->withHeader('Content-Type', 'application/json')->withStatus($status);
}

/** @return array<string,mixed> */
function body(Request $request): array
{
    $data = $request->getParsedBody();
    return is_array($data) ? $data : [];
}

function need(array $data, string $key): mixed
{
    if (!array_key_exists($key, $data) || $data[$key] === null || $data[$key] === '') {
        throw new DomainException("missing required field '{$key}'", 422, 'missing_field');
    }
    return $data[$key];
}

function operator(array $data): string
{
    return (string) ($data['operator'] ?? 'system');
}

$contracts = new ContractService();
$imports = new ImportService();
$settlements = new SettlementService();
$closes = new CloseService();

// ---------- templates ----------
$app->post('/api/templates', function (Request $req, Response $res) use ($contracts) {
    $b = body($req);
    $t = $contracts->createTemplate((string) need($b, 'name'), (string) need($b, 'body'));
    return json($res, $t->toArray(), 201);
});

$app->get('/api/templates/{id}', function (Request $req, Response $res, array $args) {
    $t = Meridian\Models\ContractTemplate::find((int) $args['id'])
        ?? throw new DomainException('template not found', 404, 'template_not_found');
    return json($res, $t->toArray());
});

// ---------- contracts & signing ----------
$app->post('/api/contracts', function (Request $req, Response $res) use ($contracts) {
    $b = body($req);
    $c = $contracts->createContract(
        (string) need($b, 'code'),
        (int) need($b, 'template_id'),
        (string) need($b, 'title'),
        (array) ($b['variables'] ?? []),
        operator($b)
    );
    return json($res, $c->toArray(), 201);
});

$app->get('/api/contracts/{id}', function (Request $req, Response $res, array $args) use ($contracts) {
    return json($res, $contracts->getContract((int) $args['id'])->toArray());
});

$app->post('/api/contracts/{id}/versions', function (Request $req, Response $res, array $args) use ($contracts) {
    $b = body($req);
    $v = $contracts->createVersion((int) $args['id'], (array) need($b, 'variables'), operator($b));
    return json($res, $v->toArray(), 201);
});

$app->post('/api/contracts/{id}/versions/{v}/initiate-sign', function (Request $req, Response $res, array $args) use ($contracts) {
    $b = body($req);
    $v = $contracts->initiateSign((int) $args['id'], (int) $args['v'], (array) need($b, 'signers'), operator($b));
    return json($res, $v->toArray());
});

$app->post('/api/contracts/{id}/versions/{v}/confirm', function (Request $req, Response $res, array $args) use ($contracts) {
    $b = body($req);
    $result = $contracts->confirm((int) $args['id'], (int) $args['v'], (int) need($b, 'seq'), operator($b));
    return json($res, [
        'idempotent' => $result['idempotent'],
        'signer' => $result['signer']->toArray(),
        'version' => $result['version']->toArray(),
    ]);
});

$app->post('/api/contracts/{id}/withdraw', function (Request $req, Response $res, array $args) use ($contracts) {
    $b = body($req);
    return json($res, $contracts->withdraw((int) $args['id'], operator($b))->toArray());
});

$app->post('/api/contracts/{id}/expire', function (Request $req, Response $res, array $args) use ($contracts) {
    $b = body($req);
    return json($res, $contracts->expire((int) $args['id'], operator($b))->toArray());
});

// ---------- settlement line imports ----------
$app->post('/api/imports', function (Request $req, Response $res) use ($imports) {
    $b = body($req);
    $batch = $imports->import((string) need($b, 'source'), (array) need($b, 'lines'), operator($b));
    return json($res, $batch->toArray(), 201);
});

$app->get('/api/imports/{id}', function (Request $req, Response $res, array $args) {
    $batch = Meridian\Models\ImportBatch::with('lines')->find((int) $args['id'])
        ?? throw new DomainException('batch not found', 404, 'batch_not_found');
    return json($res, $batch->toArray());
});

// ---------- allocation rules ----------
$app->post('/api/allocation-rules', function (Request $req, Response $res) use ($settlements) {
    $b = body($req);
    $rule = $settlements->createRuleVersion(
        (string) need($b, 'name'),
        (array) need($b, 'allocations'),
        $b['remainder_target'] ?? null,
        operator($b)
    );
    return json($res, $rule->toArray(), 201);
});

// ---------- settlements ----------
$app->post('/api/settlements', function (Request $req, Response $res) use ($settlements) {
    $b = body($req);
    $s = $settlements->generate(
        (int) need($b, 'contract_id'),
        isset($b['contract_version_id']) ? (int) $b['contract_version_id'] : null,
        (string) need($b, 'currency'),
        (string) need($b, 'business_date'),
        (int) need($b, 'rule_version_id'),
        operator($b)
    );
    return json($res, $s->toArray(), 201);
});

$app->get('/api/settlements/{id}', function (Request $req, Response $res, array $args) use ($settlements) {
    return json($res, $settlements->getSettlement((int) $args['id'])->toArray());
});

// ---------- daily close & corrections ----------
$app->post('/api/closes', function (Request $req, Response $res) use ($closes) {
    $b = body($req);
    $close = $closes->close((string) need($b, 'date'), operator($b));
    return json($res, $close->toArray(), 201);
});

$app->post('/api/adjustments', function (Request $req, Response $res) use ($closes) {
    $b = body($req);
    $adj = $closes->adjust(
        (string) need($b, 'target_type'),
        (int) need($b, 'target_id'),
        (string) need($b, 'type'),
        isset($b['amount']) ? (string) $b['amount'] : null,
        (string) need($b, 'business_date'),
        (string) need($b, 'reason'),
        operator($b)
    );
    return json($res, $adj->toArray(), 201);
});

$app->get('/health', fn (Request $req, Response $res) => json($res, ['status' => 'ok']));

$app->run();
