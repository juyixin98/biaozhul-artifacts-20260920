<?php

declare(strict_types=1);

require __DIR__ . '/../vendor/autoload.php';

use Psr\Http\Message\ResponseInterface as Response;
use Psr\Http\Message\ServerRequestInterface as Request;
use Slim\Factory\AppFactory;
use TravelOps\ApiException;
use TravelOps\Database;

Database::boot();

$app = AppFactory::create();

// JSON body parsing with a strict error for malformed payloads.
$app->addBodyParsingMiddleware();
$customError = function (Request $request, Throwable $exception, bool $displayErrorDetails) use ($app): Response {
    $response = $app->getResponseFactory()->createResponse();
    if ($exception instanceof ApiException) {
        $payload = [
            'error' => [
                'code' => $exception->errorCode,
                'message' => $exception->getMessage(),
            ] + ($exception->details ? ['details' => $exception->details] : []),
        ];
        $status = $exception->status;
    } else {
        $payload = [
            'error' => [
                'code' => 'internal_error',
                'message' => $displayErrorDetails ? $exception->getMessage() : 'internal server error',
            ],
        ];
        $status = 500;
        if (getenv('APP_DEBUG') === '1') {
            $payload['error']['trace'] = $exception->getTraceAsString();
        }
    }
    $response->getBody()->write(json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT));
    return $response
        ->withHeader('Content-Type', 'application/json')
        ->withStatus($status);
};

$errorMiddleware = $app->addErrorMiddleware(getenv('APP_DEBUG') === '1', true, true);
$errorMiddleware->setDefaultErrorHandler($customError);

$json = function (Response $response, mixed $data, int $status = 200): Response {
    $response->getBody()->write(json_encode($data, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES));
    return $response->withHeader('Content-Type', 'application/json')->withStatus($status);
};

$actor = function (Request $request): string {
    $header = $request->getHeaderLine('X-Actor');
    return $header !== '' ? $header : 'system';
};

(require __DIR__ . '/../src/routes.php')($app, $json, $actor);

$app->run();
