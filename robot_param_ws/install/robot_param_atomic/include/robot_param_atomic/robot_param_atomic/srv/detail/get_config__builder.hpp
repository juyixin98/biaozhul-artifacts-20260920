// generated from rosidl_generator_cpp/resource/idl__builder.hpp.em
// with input from robot_param_atomic:srv/GetConfig.idl
// generated code does not contain a copyright notice

// IWYU pragma: private, include "robot_param_atomic/srv/get_config.hpp"


#ifndef ROBOT_PARAM_ATOMIC__SRV__DETAIL__GET_CONFIG__BUILDER_HPP_
#define ROBOT_PARAM_ATOMIC__SRV__DETAIL__GET_CONFIG__BUILDER_HPP_

#include <algorithm>
#include <utility>

#include "robot_param_atomic/srv/detail/get_config__struct.hpp"
#include "rosidl_runtime_cpp/message_initialization.hpp"


namespace robot_param_atomic
{

namespace srv
{


}  // namespace srv

template<typename MessageType>
auto build();

template<>
inline
auto build<::robot_param_atomic::srv::GetConfig_Request>()
{
  return ::robot_param_atomic::srv::GetConfig_Request(rosidl_runtime_cpp::MessageInitialization::ZERO);
}

}  // namespace robot_param_atomic


namespace robot_param_atomic
{

namespace srv
{

namespace builder
{

class Init_GetConfig_Response_commit_seq
{
public:
  explicit Init_GetConfig_Response_commit_seq(::robot_param_atomic::srv::GetConfig_Response & msg)
  : msg_(msg)
  {}
  ::robot_param_atomic::srv::GetConfig_Response commit_seq(::robot_param_atomic::srv::GetConfig_Response::_commit_seq_type arg)
  {
    msg_.commit_seq = std::move(arg);
    return std::move(msg_);
  }

private:
  ::robot_param_atomic::srv::GetConfig_Response msg_;
};

class Init_GetConfig_Response_config_hash
{
public:
  explicit Init_GetConfig_Response_config_hash(::robot_param_atomic::srv::GetConfig_Response & msg)
  : msg_(msg)
  {}
  Init_GetConfig_Response_commit_seq config_hash(::robot_param_atomic::srv::GetConfig_Response::_config_hash_type arg)
  {
    msg_.config_hash = std::move(arg);
    return Init_GetConfig_Response_commit_seq(msg_);
  }

private:
  ::robot_param_atomic::srv::GetConfig_Response msg_;
};

class Init_GetConfig_Response_allowed_latency_s
{
public:
  explicit Init_GetConfig_Response_allowed_latency_s(::robot_param_atomic::srv::GetConfig_Response & msg)
  : msg_(msg)
  {}
  Init_GetConfig_Response_config_hash allowed_latency_s(::robot_param_atomic::srv::GetConfig_Response::_allowed_latency_s_type arg)
  {
    msg_.allowed_latency_s = std::move(arg);
    return Init_GetConfig_Response_config_hash(msg_);
  }

private:
  ::robot_param_atomic::srv::GetConfig_Response msg_;
};

class Init_GetConfig_Response_cache_length_s
{
public:
  explicit Init_GetConfig_Response_cache_length_s(::robot_param_atomic::srv::GetConfig_Response & msg)
  : msg_(msg)
  {}
  Init_GetConfig_Response_allowed_latency_s cache_length_s(::robot_param_atomic::srv::GetConfig_Response::_cache_length_s_type arg)
  {
    msg_.cache_length_s = std::move(arg);
    return Init_GetConfig_Response_allowed_latency_s(msg_);
  }

private:
  ::robot_param_atomic::srv::GetConfig_Response msg_;
};

class Init_GetConfig_Response_sampling_rate_hz
{
public:
  explicit Init_GetConfig_Response_sampling_rate_hz(::robot_param_atomic::srv::GetConfig_Response & msg)
  : msg_(msg)
  {}
  Init_GetConfig_Response_cache_length_s sampling_rate_hz(::robot_param_atomic::srv::GetConfig_Response::_sampling_rate_hz_type arg)
  {
    msg_.sampling_rate_hz = std::move(arg);
    return Init_GetConfig_Response_cache_length_s(msg_);
  }

private:
  ::robot_param_atomic::srv::GetConfig_Response msg_;
};

class Init_GetConfig_Response_version
{
public:
  Init_GetConfig_Response_version()
  : msg_(::rosidl_runtime_cpp::MessageInitialization::SKIP)
  {}
  Init_GetConfig_Response_sampling_rate_hz version(::robot_param_atomic::srv::GetConfig_Response::_version_type arg)
  {
    msg_.version = std::move(arg);
    return Init_GetConfig_Response_sampling_rate_hz(msg_);
  }

private:
  ::robot_param_atomic::srv::GetConfig_Response msg_;
};

}  // namespace builder

}  // namespace srv

template<typename MessageType>
auto build();

template<>
inline
auto build<::robot_param_atomic::srv::GetConfig_Response>()
{
  return robot_param_atomic::srv::builder::Init_GetConfig_Response_version();
}

}  // namespace robot_param_atomic


namespace robot_param_atomic
{

namespace srv
{

namespace builder
{

class Init_GetConfig_Event_response
{
public:
  explicit Init_GetConfig_Event_response(::robot_param_atomic::srv::GetConfig_Event & msg)
  : msg_(msg)
  {}
  ::robot_param_atomic::srv::GetConfig_Event response(::robot_param_atomic::srv::GetConfig_Event::_response_type arg)
  {
    msg_.response = std::move(arg);
    return std::move(msg_);
  }

private:
  ::robot_param_atomic::srv::GetConfig_Event msg_;
};

class Init_GetConfig_Event_request
{
public:
  explicit Init_GetConfig_Event_request(::robot_param_atomic::srv::GetConfig_Event & msg)
  : msg_(msg)
  {}
  Init_GetConfig_Event_response request(::robot_param_atomic::srv::GetConfig_Event::_request_type arg)
  {
    msg_.request = std::move(arg);
    return Init_GetConfig_Event_response(msg_);
  }

private:
  ::robot_param_atomic::srv::GetConfig_Event msg_;
};

class Init_GetConfig_Event_info
{
public:
  Init_GetConfig_Event_info()
  : msg_(::rosidl_runtime_cpp::MessageInitialization::SKIP)
  {}
  Init_GetConfig_Event_request info(::robot_param_atomic::srv::GetConfig_Event::_info_type arg)
  {
    msg_.info = std::move(arg);
    return Init_GetConfig_Event_request(msg_);
  }

private:
  ::robot_param_atomic::srv::GetConfig_Event msg_;
};

}  // namespace builder

}  // namespace srv

template<typename MessageType>
auto build();

template<>
inline
auto build<::robot_param_atomic::srv::GetConfig_Event>()
{
  return robot_param_atomic::srv::builder::Init_GetConfig_Event_info();
}

}  // namespace robot_param_atomic

#endif  // ROBOT_PARAM_ATOMIC__SRV__DETAIL__GET_CONFIG__BUILDER_HPP_
