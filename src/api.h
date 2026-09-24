// api.h — request validation and JSON <-> ICP translation.
#pragma once
#include "http_server.h"

namespace pcr {

// POST /v1/register
HttpResponse handleRegister(const HttpRequest& req);

// POST /v1/verify-hash — echoes the SHA-256 of the received body so clients can
// prove the cryptographic operation is really executed server-side.
HttpResponse handleVerifyHash(const HttpRequest& req);

// GET /healthz
HttpResponse handleHealth(const HttpRequest& req);

}  // namespace pcr
