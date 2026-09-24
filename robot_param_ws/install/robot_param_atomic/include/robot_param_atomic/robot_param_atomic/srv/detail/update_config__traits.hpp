// generated from rosidl_generator_cpp/resource/idl__traits.hpp.em
// with input from robot_param_atomic:srv/UpdateConfig.idl
// generated code does not contain a copyright notice

// IWYU pragma: private, include "robot_param_atomic/srv/update_config.hpp"


#ifndef ROBOT_PARAM_ATOMIC__SRV__DETAIL__UPDATE_CONFIG__TRAITS_HPP_
#define ROBOT_PARAM_ATOMIC__SRV__DETAIL__UPDATE_CONFIG__TRAITS_HPP_

#include <stdint.h>

#include <sstream>
#include <string>
#include <type_traits>

#include "robot_param_atomic/srv/detail/update_config__struct.hpp"
#include "rosidl_runtime_cpp/traits.hpp"

namespace robot_param_atomic
{

namespace srv
{

inline void to_flow_style_yaml(
  const UpdateConfig_Request & msg,
  std::ostream & out)
{
  out << "{";
  // member: expected_version
  {
    out << "expected_version: ";
    rosidl_generator_traits::value_to_yaml(msg.expected_version, out);
    out << ", ";
  }

  // member: sampling_rate_hz
  {
    out << "sampling_rate_hz: ";
    rosidl_generator_traits::value_to_yaml(msg.sampling_rate_hz, out);
    out << ", ";
  }

  // member: cache_length_s
  {
    out << "cache_length_s: ";
    rosidl_generator_traits::value_to_yaml(msg.cache_length_s, out);
    out << ", ";
  }

  // member: allowed_latency_s
  {
    out << "allowed_latency_s: ";
    rosidl_generator_traits::value_to_yaml(msg.allowed_latency_s, out);
  }
  out << "}";
}  // NOLINT(readability/fn_size)

inline void to_block_style_yaml(
  const UpdateConfig_Request & msg,
  std::ostream & out, size_t indentation = 0)
{
  // member: expected_version
  {
    if (indentation > 0) {
      out << std::string(indentation, ' ');
    }
    out << "expected_version: ";
    rosidl_generator_traits::value_to_yaml(msg.expected_version, out);
    out << "\n";
  }

  // member: sampling_rate_hz
  {
    if (indentation > 0) {
      out << std::string(indentation, ' ');
    }
    out << "sampling_rate_hz: ";
    rosidl_generator_traits::value_to_yaml(msg.sampling_rate_hz, out);
    out << "\n";
  }

  // member: cache_length_s
  {
    if (indentation > 0) {
      out << std::string(indentation, ' ');
    }
    out << "cache_length_s: ";
    rosidl_generator_traits::value_to_yaml(msg.cache_length_s, out);
    out << "\n";
  }

  // member: allowed_latency_s
  {
    if (indentation > 0) {
      out << std::string(indentation, ' ');
    }
    out << "allowed_latency_s: ";
    rosidl_generator_traits::value_to_yaml(msg.allowed_latency_s, out);
    out << "\n";
  }
}  // NOLINT(readability/fn_size)

inline std::string to_yaml(const UpdateConfig_Request & msg, bool use_flow_style = false)
{
  std::ostringstream out;
  if (use_flow_style) {
    to_flow_style_yaml(msg, out);
  } else {
    to_block_style_yaml(msg, out);
  }
  return out.str();
}

}  // namespace srv

}  // namespace robot_param_atomic

namespace rosidl_generator_traits
{

[[deprecated("use robot_param_atomic::srv::to_block_style_yaml() instead")]]
inline void to_yaml(
  const robot_param_atomic::srv::UpdateConfig_Request & msg,
  std::ostream & out, size_t indentation = 0)
{
  robot_param_atomic::srv::to_block_style_yaml(msg, out, indentation);
}

[[deprecated("use robot_param_atomic::srv::to_yaml() instead")]]
inline std::string to_yaml(const robot_param_atomic::srv::UpdateConfig_Request & msg)
{
  return robot_param_atomic::srv::to_yaml(msg);
}

template<>
inline const char * data_type<robot_param_atomic::srv::UpdateConfig_Request>()
{
  return "robot_param_atomic::srv::UpdateConfig_Request";
}

template<>
inline const char * name<robot_param_atomic::srv::UpdateConfig_Request>()
{
  return "robot_param_atomic/srv/UpdateConfig_Request";
}

template<>
struct has_fixed_size<robot_param_atomic::srv::UpdateConfig_Request>
  : std::integral_constant<bool, true> {};

template<>
struct has_bounded_size<robot_param_atomic::srv::UpdateConfig_Request>
  : std::integral_constant<bool, true> {};

template<>
struct is_message<robot_param_atomic::srv::UpdateConfig_Request>
  : std::true_type {};

}  // namespace rosidl_generator_traits

namespace robot_param_atomic
{

namespace srv
{

inline void to_flow_style_yaml(
  const UpdateConfig_Response & msg,
  std::ostream & out)
{
  out << "{";
  // member: ok
  {
    out << "ok: ";
    rosidl_generator_traits::value_to_yaml(msg.ok, out);
    out << ", ";
  }

  // member: code
  {
    out << "code: ";
    rosidl_generator_traits::value_to_yaml(msg.code, out);
    out << ", ";
  }

  // member: message
  {
    out << "message: ";
    rosidl_generator_traits::value_to_yaml(msg.message, out);
    out << ", ";
  }

  // member: version
  {
    out << "version: ";
    rosidl_generator_traits::value_to_yaml(msg.version, out);
    out << ", ";
  }

  // member: sampling_rate_hz
  {
    out << "sampling_rate_hz: ";
    rosidl_generator_traits::value_to_yaml(msg.sampling_rate_hz, out);
    out << ", ";
  }

  // member: cache_length_s
  {
    out << "cache_length_s: ";
    rosidl_generator_traits::value_to_yaml(msg.cache_length_s, out);
    out << ", ";
  }

  // member: allowed_latency_s
  {
    out << "allowed_latency_s: ";
    rosidl_generator_traits::value_to_yaml(msg.allowed_latency_s, out);
  }
  out << "}";
}  // NOLINT(readability/fn_size)

inline void to_block_style_yaml(
  const UpdateConfig_Response & msg,
  std::ostream & out, size_t indentation = 0)
{
  // member: ok
  {
    if (indentation > 0) {
      out << std::string(indentation, ' ');
    }
    out << "ok: ";
    rosidl_generator_traits::value_to_yaml(msg.ok, out);
    out << "\n";
  }

  // member: code
  {
    if (indentation > 0) {
      out << std::string(indentation, ' ');
    }
    out << "code: ";
    rosidl_generator_traits::value_to_yaml(msg.code, out);
    out << "\n";
  }

  // member: message
  {
    if (indentation > 0) {
      out << std::string(indentation, ' ');
    }
    out << "message: ";
    rosidl_generator_traits::value_to_yaml(msg.message, out);
    out << "\n";
  }

  // member: version
  {
    if (indentation > 0) {
      out << std::string(indentation, ' ');
    }
    out << "version: ";
    rosidl_generator_traits::value_to_yaml(msg.version, out);
    out << "\n";
  }

  // member: sampling_rate_hz
  {
    if (indentation > 0) {
      out << std::string(indentation, ' ');
    }
    out << "sampling_rate_hz: ";
    rosidl_generator_traits::value_to_yaml(msg.sampling_rate_hz, out);
    out << "\n";
  }

  // member: cache_length_s
  {
    if (indentation > 0) {
      out << std::string(indentation, ' ');
    }
    out << "cache_length_s: ";
    rosidl_generator_traits::value_to_yaml(msg.cache_length_s, out);
    out << "\n";
  }

  // member: allowed_latency_s
  {
    if (indentation > 0) {
      out << std::string(indentation, ' ');
    }
    out << "allowed_latency_s: ";
    rosidl_generator_traits::value_to_yaml(msg.allowed_latency_s, out);
    out << "\n";
  }
}  // NOLINT(readability/fn_size)

inline std::string to_yaml(const UpdateConfig_Response & msg, bool use_flow_style = false)
{
  std::ostringstream out;
  if (use_flow_style) {
    to_flow_style_yaml(msg, out);
  } else {
    to_block_style_yaml(msg, out);
  }
  return out.str();
}

}  // namespace srv

}  // namespace robot_param_atomic

namespace rosidl_generator_traits
{

[[deprecated("use robot_param_atomic::srv::to_block_style_yaml() instead")]]
inline void to_yaml(
  const robot_param_atomic::srv::UpdateConfig_Response & msg,
  std::ostream & out, size_t indentation = 0)
{
  robot_param_atomic::srv::to_block_style_yaml(msg, out, indentation);
}

[[deprecated("use robot_param_atomic::srv::to_yaml() instead")]]
inline std::string to_yaml(const robot_param_atomic::srv::UpdateConfig_Response & msg)
{
  return robot_param_atomic::srv::to_yaml(msg);
}

template<>
inline const char * data_type<robot_param_atomic::srv::UpdateConfig_Response>()
{
  return "robot_param_atomic::srv::UpdateConfig_Response";
}

template<>
inline const char * name<robot_param_atomic::srv::UpdateConfig_Response>()
{
  return "robot_param_atomic/srv/UpdateConfig_Response";
}

template<>
struct has_fixed_size<robot_param_atomic::srv::UpdateConfig_Response>
  : std::integral_constant<bool, false> {};

template<>
struct has_bounded_size<robot_param_atomic::srv::UpdateConfig_Response>
  : std::integral_constant<bool, false> {};

template<>
struct is_message<robot_param_atomic::srv::UpdateConfig_Response>
  : std::true_type {};

}  // namespace rosidl_generator_traits

// Include directives for member types
// Member 'info'
#include "service_msgs/msg/detail/service_event_info__traits.hpp"

namespace robot_param_atomic
{

namespace srv
{

inline void to_flow_style_yaml(
  const UpdateConfig_Event & msg,
  std::ostream & out)
{
  out << "{";
  // member: info
  {
    out << "info: ";
    to_flow_style_yaml(msg.info, out);
    out << ", ";
  }

  // member: request
  {
    if (msg.request.size() == 0) {
      out << "request: []";
    } else {
      out << "request: [";
      size_t pending_items = msg.request.size();
      for (auto item : msg.request) {
        to_flow_style_yaml(item, out);
        if (--pending_items > 0) {
          out << ", ";
        }
      }
      out << "]";
    }
    out << ", ";
  }

  // member: response
  {
    if (msg.response.size() == 0) {
      out << "response: []";
    } else {
      out << "response: [";
      size_t pending_items = msg.response.size();
      for (auto item : msg.response) {
        to_flow_style_yaml(item, out);
        if (--pending_items > 0) {
          out << ", ";
        }
      }
      out << "]";
    }
  }
  out << "}";
}  // NOLINT(readability/fn_size)

inline void to_block_style_yaml(
  const UpdateConfig_Event & msg,
  std::ostream & out, size_t indentation = 0)
{
  // member: info
  {
    if (indentation > 0) {
      out << std::string(indentation, ' ');
    }
    out << "info:\n";
    to_block_style_yaml(msg.info, out, indentation + 2);
  }

  // member: request
  {
    if (indentation > 0) {
      out << std::string(indentation, ' ');
    }
    if (msg.request.size() == 0) {
      out << "request: []\n";
    } else {
      out << "request:\n";
      for (auto item : msg.request) {
        if (indentation > 0) {
          out << std::string(indentation, ' ');
        }
        out << "-\n";
        to_block_style_yaml(item, out, indentation + 2);
      }
    }
  }

  // member: response
  {
    if (indentation > 0) {
      out << std::string(indentation, ' ');
    }
    if (msg.response.size() == 0) {
      out << "response: []\n";
    } else {
      out << "response:\n";
      for (auto item : msg.response) {
        if (indentation > 0) {
          out << std::string(indentation, ' ');
        }
        out << "-\n";
        to_block_style_yaml(item, out, indentation + 2);
      }
    }
  }
}  // NOLINT(readability/fn_size)

inline std::string to_yaml(const UpdateConfig_Event & msg, bool use_flow_style = false)
{
  std::ostringstream out;
  if (use_flow_style) {
    to_flow_style_yaml(msg, out);
  } else {
    to_block_style_yaml(msg, out);
  }
  return out.str();
}

}  // namespace srv

}  // namespace robot_param_atomic

namespace rosidl_generator_traits
{

[[deprecated("use robot_param_atomic::srv::to_block_style_yaml() instead")]]
inline void to_yaml(
  const robot_param_atomic::srv::UpdateConfig_Event & msg,
  std::ostream & out, size_t indentation = 0)
{
  robot_param_atomic::srv::to_block_style_yaml(msg, out, indentation);
}

[[deprecated("use robot_param_atomic::srv::to_yaml() instead")]]
inline std::string to_yaml(const robot_param_atomic::srv::UpdateConfig_Event & msg)
{
  return robot_param_atomic::srv::to_yaml(msg);
}

template<>
inline const char * data_type<robot_param_atomic::srv::UpdateConfig_Event>()
{
  return "robot_param_atomic::srv::UpdateConfig_Event";
}

template<>
inline const char * name<robot_param_atomic::srv::UpdateConfig_Event>()
{
  return "robot_param_atomic/srv/UpdateConfig_Event";
}

template<>
struct has_fixed_size<robot_param_atomic::srv::UpdateConfig_Event>
  : std::integral_constant<bool, false> {};

template<>
struct has_bounded_size<robot_param_atomic::srv::UpdateConfig_Event>
  : std::integral_constant<bool, has_bounded_size<robot_param_atomic::srv::UpdateConfig_Request>::value && has_bounded_size<robot_param_atomic::srv::UpdateConfig_Response>::value && has_bounded_size<service_msgs::msg::ServiceEventInfo>::value> {};

template<>
struct is_message<robot_param_atomic::srv::UpdateConfig_Event>
  : std::true_type {};

}  // namespace rosidl_generator_traits

namespace rosidl_generator_traits
{

template<>
inline const char * data_type<robot_param_atomic::srv::UpdateConfig>()
{
  return "robot_param_atomic::srv::UpdateConfig";
}

template<>
inline const char * name<robot_param_atomic::srv::UpdateConfig>()
{
  return "robot_param_atomic/srv/UpdateConfig";
}

template<>
struct has_fixed_size<robot_param_atomic::srv::UpdateConfig>
  : std::integral_constant<
    bool,
    has_fixed_size<robot_param_atomic::srv::UpdateConfig_Request>::value &&
    has_fixed_size<robot_param_atomic::srv::UpdateConfig_Response>::value
  >
{
};

template<>
struct has_bounded_size<robot_param_atomic::srv::UpdateConfig>
  : std::integral_constant<
    bool,
    has_bounded_size<robot_param_atomic::srv::UpdateConfig_Request>::value &&
    has_bounded_size<robot_param_atomic::srv::UpdateConfig_Response>::value
  >
{
};

template<>
struct is_service<robot_param_atomic::srv::UpdateConfig>
  : std::true_type
{
};

template<>
struct is_service_request<robot_param_atomic::srv::UpdateConfig_Request>
  : std::true_type
{
};

template<>
struct is_service_response<robot_param_atomic::srv::UpdateConfig_Response>
  : std::true_type
{
};

}  // namespace rosidl_generator_traits

#endif  // ROBOT_PARAM_ATOMIC__SRV__DETAIL__UPDATE_CONFIG__TRAITS_HPP_
