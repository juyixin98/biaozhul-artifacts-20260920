// generated from rosidl_generator_cpp/resource/idl__builder.hpp.em
// with input from robot_param_atomic:srv/UpdateConfig.idl
// generated code does not contain a copyright notice

// IWYU pragma: private, include "robot_param_atomic/srv/update_config.hpp"


#ifndef ROBOT_PARAM_ATOMIC__SRV__DETAIL__UPDATE_CONFIG__BUILDER_HPP_
#define ROBOT_PARAM_ATOMIC__SRV__DETAIL__UPDATE_CONFIG__BUILDER_HPP_

#include <algorithm>
#include <utility>

#include "robot_param_atomic/srv/detail/update_config__struct.hpp"
#include "rosidl_runtime_cpp/message_initialization.hpp"


namespace robot_param_atomic
{

namespace srv
{

namespace builder
{

class Init_UpdateConfig_Request_allowed_latency_s
{
public:
  explicit Init_UpdateConfig_Request_allowed_latency_s(::robot_param_atomic::srv::UpdateConfig_Request & msg)
  : msg_(msg)
  {}
  ::robot_param_atomic::srv::UpdateConfig_Request allowed_latency_s(::robot_param_atomic::srv::UpdateConfig_Request::_allowed_latency_s_type arg)
  {
    msg_.allowed_latency_s = std::move(arg);
    return std::move(msg_);
  }

private:
  ::robot_param_atomic::srv::UpdateConfig_Request msg_;
};

class Init_UpdateConfig_Request_cache_length_s
{
public:
  explicit Init_UpdateConfig_Request_cache_length_s(::robot_param_atomic::srv::UpdateConfig_Request & msg)
  : msg_(msg)
  {}
  Init_UpdateConfig_Request_allowed_latency_s cache_length_s(::robot_param_atomic::srv::UpdateConfig_Request::_cache_length_s_type arg)
  {
    msg_.cache_length_s = std::move(arg);
    return Init_UpdateConfig_Request_allowed_latency_s(msg_);
  }

private:
  ::robot_param_atomic::srv::UpdateConfig_Request msg_;
};

class Init_UpdateConfig_Request_sampling_rate_hz
{
public:
  explicit Init_UpdateConfig_Request_sampling_rate_hz(::robot_param_atomic::srv::UpdateConfig_Request & msg)
  : msg_(msg)
  {}
  Init_UpdateConfig_Request_cache_length_s sampling_rate_hz(::robot_param_atomic::srv::UpdateConfig_Request::_sampling_rate_hz_type arg)
  {
    msg_.sampling_rate_hz = std::move(arg);
    return Init_UpdateConfig_Request_cache_length_s(msg_);
  }

private:
  ::robot_param_atomic::srv::UpdateConfig_Request msg_;
};

class Init_UpdateConfig_Request_expected_version
{
public:
  Init_UpdateConfig_Request_expected_version()
  : msg_(::rosidl_runtime_cpp::MessageInitialization::SKIP)
  {}
  Init_UpdateConfig_Request_sampling_rate_hz expected_version(::robot_param_atomic::srv::UpdateConfig_Request::_expected_version_type arg)
  {
    msg_.expected_version = std::move(arg);
    return Init_UpdateConfig_Request_sampling_rate_hz(msg_);
  }

private:
  ::robot_param_atomic::srv::UpdateConfig_Request msg_;
};

}  // namespace builder

}  // namespace srv

template<typename MessageType>
auto build();

template<>
inline
auto build<::robot_param_atomic::srv::UpdateConfig_Request>()
{
  return robot_param_atomic::srv::builder::Init_UpdateConfig_Request_expected_version();
}

}  // namespace robot_param_atomic


namespace robot_param_atomic
{

namespace srv
{

namespace builder
{

class Init_UpdateConfig_Response_allowed_latency_s
{
public:
  explicit Init_UpdateConfig_Response_allowed_latency_s(::robot_param_atomic::srv::UpdateConfig_Response & msg)
  : msg_(msg)
  {}
  ::robot_param_atomic::srv::UpdateConfig_Response allowed_latency_s(::robot_param_atomic::srv::UpdateConfig_Response::_allowed_latency_s_type arg)
  {
    msg_.allowed_latency_s = std::move(arg);
    return std::move(msg_);
  }

private:
  ::robot_param_atomic::srv::UpdateConfig_Response msg_;
};

class Init_UpdateConfig_Response_cache_length_s
{
public:
  explicit Init_UpdateConfig_Response_cache_length_s(::robot_param_atomic::srv::UpdateConfig_Response & msg)
  : msg_(msg)
  {}
  Init_UpdateConfig_Response_allowed_latency_s cache_length_s(::robot_param_atomic::srv::UpdateConfig_Response::_cache_length_s_type arg)
  {
    msg_.cache_length_s = std::move(arg);
    return Init_UpdateConfig_Response_allowed_latency_s(msg_);
  }

private:
  ::robot_param_atomic::srv::UpdateConfig_Response msg_;
};

class Init_UpdateConfig_Response_sampling_rate_hz
{
public:
  explicit Init_UpdateConfig_Response_sampling_rate_hz(::robot_param_atomic::srv::UpdateConfig_Response & msg)
  : msg_(msg)
  {}
  Init_UpdateConfig_Response_cache_length_s sampling_rate_hz(::robot_param_atomic::srv::UpdateConfig_Response::_sampling_rate_hz_type arg)
  {
    msg_.sampling_rate_hz = std::move(arg);
    return Init_UpdateConfig_Response_cache_length_s(msg_);
  }

private:
  ::robot_param_atomic::srv::UpdateConfig_Response msg_;
};

class Init_UpdateConfig_Response_version
{
public:
  explicit Init_UpdateConfig_Response_version(::robot_param_atomic::srv::UpdateConfig_Response & msg)
  : msg_(msg)
  {}
  Init_UpdateConfig_Response_sampling_rate_hz version(::robot_param_atomic::srv::UpdateConfig_Response::_version_type arg)
  {
    msg_.version = std::move(arg);
    return Init_UpdateConfig_Response_sampling_rate_hz(msg_);
  }

private:
  ::robot_param_atomic::srv::UpdateConfig_Response msg_;
};

class Init_UpdateConfig_Response_message
{
public:
  explicit Init_UpdateConfig_Response_message(::robot_param_atomic::srv::UpdateConfig_Response & msg)
  : msg_(msg)
  {}
  Init_UpdateConfig_Response_version message(::robot_param_atomic::srv::UpdateConfig_Response::_message_type arg)
  {
    msg_.message = std::move(arg);
    return Init_UpdateConfig_Response_version(msg_);
  }

private:
  ::robot_param_atomic::srv::UpdateConfig_Response msg_;
};

class Init_UpdateConfig_Response_code
{
public:
  explicit Init_UpdateConfig_Response_code(::robot_param_atomic::srv::UpdateConfig_Response & msg)
  : msg_(msg)
  {}
  Init_UpdateConfig_Response_message code(::robot_param_atomic::srv::UpdateConfig_Response::_code_type arg)
  {
    msg_.code = std::move(arg);
    return Init_UpdateConfig_Response_message(msg_);
  }

private:
  ::robot_param_atomic::srv::UpdateConfig_Response msg_;
};

class Init_UpdateConfig_Response_ok
{
public:
  Init_UpdateConfig_Response_ok()
  : msg_(::rosidl_runtime_cpp::MessageInitialization::SKIP)
  {}
  Init_UpdateConfig_Response_code ok(::robot_param_atomic::srv::UpdateConfig_Response::_ok_type arg)
  {
    msg_.ok = std::move(arg);
    return Init_UpdateConfig_Response_code(msg_);
  }

private:
  ::robot_param_atomic::srv::UpdateConfig_Response msg_;
};

}  // namespace builder

}  // namespace srv

template<typename MessageType>
auto build();

template<>
inline
auto build<::robot_param_atomic::srv::UpdateConfig_Response>()
{
  return robot_param_atomic::srv::builder::Init_UpdateConfig_Response_ok();
}

}  // namespace robot_param_atomic


namespace robot_param_atomic
{

namespace srv
{

namespace builder
{

class Init_UpdateConfig_Event_response
{
public:
  explicit Init_UpdateConfig_Event_response(::robot_param_atomic::srv::UpdateConfig_Event & msg)
  : msg_(msg)
  {}
  ::robot_param_atomic::srv::UpdateConfig_Event response(::robot_param_atomic::srv::UpdateConfig_Event::_response_type arg)
  {
    msg_.response = std::move(arg);
    return std::move(msg_);
  }

private:
  ::robot_param_atomic::srv::UpdateConfig_Event msg_;
};

class Init_UpdateConfig_Event_request
{
public:
  explicit Init_UpdateConfig_Event_request(::robot_param_atomic::srv::UpdateConfig_Event & msg)
  : msg_(msg)
  {}
  Init_UpdateConfig_Event_response request(::robot_param_atomic::srv::UpdateConfig_Event::_request_type arg)
  {
    msg_.request = std::move(arg);
    return Init_UpdateConfig_Event_response(msg_);
  }

private:
  ::robot_param_atomic::srv::UpdateConfig_Event msg_;
};

class Init_UpdateConfig_Event_info
{
public:
  Init_UpdateConfig_Event_info()
  : msg_(::rosidl_runtime_cpp::MessageInitialization::SKIP)
  {}
  Init_UpdateConfig_Event_request info(::robot_param_atomic::srv::UpdateConfig_Event::_info_type arg)
  {
    msg_.info = std::move(arg);
    return Init_UpdateConfig_Event_request(msg_);
  }

private:
  ::robot_param_atomic::srv::UpdateConfig_Event msg_;
};

}  // namespace builder

}  // namespace srv

template<typename MessageType>
auto build();

template<>
inline
auto build<::robot_param_atomic::srv::UpdateConfig_Event>()
{
  return robot_param_atomic::srv::builder::Init_UpdateConfig_Event_info();
}

}  // namespace robot_param_atomic

#endif  // ROBOT_PARAM_ATOMIC__SRV__DETAIL__UPDATE_CONFIG__BUILDER_HPP_
