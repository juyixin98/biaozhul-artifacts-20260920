#include "canonical.h"

#include <algorithm>
#include <cmath>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <vector>

namespace pgo {
namespace {

void EscapeString(const std::string& s, std::string* out) {
  out->push_back('"');
  for (unsigned char c : s) {
    switch (c) {
      case '"':  out->append("\\\""); break;
      case '\\': out->append("\\\\"); break;
      case '\b': out->append("\\b"); break;
      case '\f': out->append("\\f"); break;
      case '\n': out->append("\\n"); break;
      case '\r': out->append("\\r"); break;
      case '\t': out->append("\\t"); break;
      default:
        if (c < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          out->append(buf);
        } else {
          out->push_back(static_cast<char>(c));
        }
    }
  }
  out->push_back('"');
}

void Render(const nlohmann::json& j, int indent, std::string* out) {
  using nlohmann::json;
  const std::string pad(static_cast<size_t>(indent * 2), ' ');
  const std::string child(static_cast<size_t>((indent + 1) * 2), ' ');

  switch (j.type()) {
    case json::value_t::null:
      out->append("null");
      break;
    case json::value_t::boolean:
      out->append(j.get<bool>() ? "true" : "false");
      break;
    case json::value_t::number_integer:
    case json::value_t::number_unsigned:
      out->append(std::to_string(j.get<int64_t>()));
      break;
    case json::value_t::number_float: {
      double v = j.get<double>();
      if (!std::isfinite(v))
        throw std::runtime_error("non-finite number in canonical JSON");
      std::ostringstream os;
      os << std::setprecision(17) << v;
      std::string num = os.str();
      // normalize -0
      if (num == "-0") num = "0";
      out->append(num);
      break;
    }
    case json::value_t::string:
      EscapeString(j.get<std::string>(), out);
      break;
    case json::value_t::array: {
      if (j.empty()) { out->append("[]"); break; }
      out->push_back('[');
      bool first = true;
      for (const auto& el : j) {
        if (!first) out->push_back(',');
        first = false;
        out->push_back('\n');
        out->append(child);
        Render(el, indent + 1, out);
      }
      out->push_back('\n');
      out->append(pad);
      out->push_back(']');
      break;
    }
    case json::value_t::object: {
      if (j.empty()) { out->append("{}"); break; }
      std::vector<std::string> keys;
      keys.reserve(j.size());
      for (auto it = j.begin(); it != j.end(); ++it) keys.push_back(it.key());
      std::sort(keys.begin(), keys.end());
      out->push_back('{');
      bool first = true;
      for (const auto& k : keys) {
        if (!first) out->push_back(',');
        first = false;
        out->push_back('\n');
        out->append(child);
        EscapeString(k, out);
        out->append(": ");
        Render(j.at(k), indent + 1, out);
      }
      out->push_back('\n');
      out->append(pad);
      out->push_back('}');
      break;
    }
    default:
      throw std::runtime_error("binary/discarded value in canonical JSON");
  }
}

}  // namespace

std::string CanonicalJson(const nlohmann::json& j) {
  std::string out;
  Render(j, 0, &out);
  out.push_back('\n');
  return out;
}

bool ConstantTimeEquals(const std::string& a, const std::string& b) {
  if (a.size() != b.size()) return false;
  unsigned char diff = 0;
  for (size_t i = 0; i < a.size(); ++i)
    diff |= static_cast<unsigned char>(a[i]) ^ static_cast<unsigned char>(b[i]);
  return diff == 0;
}

}  // namespace pgo
