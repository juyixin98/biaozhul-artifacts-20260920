#pragma once
#include "topp/json.hpp"
#include "topp/types.hpp"
#include <string>

namespace topp {

struct HttpReply {
    int status;                 // 200 / 400 / 422 / 500
    std::string contentType;
    std::string body;           // canonical JSON (includes checksum on 200)
};

// Parse a /parameterize request body, run the pipeline, build the reply.
HttpReply handleParameterize(const std::string& body);

// Static health reply (also checksummed so operators can verify the crypto
// path end to end).
HttpReply handleHealth();

} // namespace topp
