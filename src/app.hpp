// Application layer: JSON-over-HTTP protocol, HMAC request authentication
// and mapping onto the FrameTree.
#pragma once

#include "frame_tree.hpp"
#include "http_server.hpp"

#include <cstdint>
#include <string>

namespace tf {

class App {
 public:
  App(std::string hmac_key, int64_t timestamp_tolerance_sec);

  HttpResponse dispatch(const HttpRequest& req);
  FrameTree& tree() { return tree_; }

  // Canonical string that is signed:
  //   METHOD\nRAW_TARGET (path?query)\nTIMESTAMP\nRAW_BODY
  static std::string signingPayload(const std::string& method,
                                    const std::string& path,
                                    const std::string& timestamp,
                                    const std::string& raw_body);

 private:
  HttpResponse handleHealth(const HttpRequest& req);
  HttpResponse handleAddStatic(const HttpRequest& req);
  HttpResponse handleAddSamples(const HttpRequest& req);
  HttpResponse handleQuery(const HttpRequest& req);
  HttpResponse handleTree(const HttpRequest& req);

  HttpResponse auth(const HttpRequest& req);  // 200-ish sentinel on success

  FrameTree tree_;
  std::string hmac_key_;
  int64_t ts_tol_sec_;
};

}  // namespace tf
