// generated from rosidl_generator_cpp/resource/idl__struct.hpp.em
// with input from robot_param_atomic:srv/UpdateConfig.idl
// generated code does not contain a copyright notice

// IWYU pragma: private, include "robot_param_atomic/srv/update_config.hpp"


#ifndef ROBOT_PARAM_ATOMIC__SRV__DETAIL__UPDATE_CONFIG__STRUCT_HPP_
#define ROBOT_PARAM_ATOMIC__SRV__DETAIL__UPDATE_CONFIG__STRUCT_HPP_

#include <algorithm>
#include <array>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "rosidl_runtime_cpp/bounded_vector.hpp"
#include "rosidl_runtime_cpp/message_initialization.hpp"


#ifndef _WIN32
# define DEPRECATED__robot_param_atomic__srv__UpdateConfig_Request __attribute__((deprecated))
#else
# define DEPRECATED__robot_param_atomic__srv__UpdateConfig_Request __declspec(deprecated)
#endif

namespace robot_param_atomic
{

namespace srv
{

// message struct
template<class ContainerAllocator>
struct UpdateConfig_Request_
{
  using Type = UpdateConfig_Request_<ContainerAllocator>;

  explicit UpdateConfig_Request_(rosidl_runtime_cpp::MessageInitialization _init = rosidl_runtime_cpp::MessageInitialization::ALL)
  {
    if (rosidl_runtime_cpp::MessageInitialization::ALL == _init ||
      rosidl_runtime_cpp::MessageInitialization::ZERO == _init)
    {
      this->expected_version = 0.0;
      this->sampling_rate_hz = 0.0;
      this->cache_length_s = 0.0;
      this->allowed_latency_s = 0.0;
    }
  }

  explicit UpdateConfig_Request_(const ContainerAllocator & _alloc, rosidl_runtime_cpp::MessageInitialization _init = rosidl_runtime_cpp::MessageInitialization::ALL)
  {
    (void)_alloc;
    if (rosidl_runtime_cpp::MessageInitialization::ALL == _init ||
      rosidl_runtime_cpp::MessageInitialization::ZERO == _init)
    {
      this->expected_version = 0.0;
      this->sampling_rate_hz = 0.0;
      this->cache_length_s = 0.0;
      this->allowed_latency_s = 0.0;
    }
  }

  // field types and members
  using _expected_version_type =
    double;
  _expected_version_type expected_version;
  using _sampling_rate_hz_type =
    double;
  _sampling_rate_hz_type sampling_rate_hz;
  using _cache_length_s_type =
    double;
  _cache_length_s_type cache_length_s;
  using _allowed_latency_s_type =
    double;
  _allowed_latency_s_type allowed_latency_s;

  // setters for named parameter idiom
  Type & set__expected_version(
    const double & _arg)
  {
    this->expected_version = _arg;
    return *this;
  }
  Type & set__sampling_rate_hz(
    const double & _arg)
  {
    this->sampling_rate_hz = _arg;
    return *this;
  }
  Type & set__cache_length_s(
    const double & _arg)
  {
    this->cache_length_s = _arg;
    return *this;
  }
  Type & set__allowed_latency_s(
    const double & _arg)
  {
    this->allowed_latency_s = _arg;
    return *this;
  }

  // constant declarations

  // pointer types
  using RawPtr =
    robot_param_atomic::srv::UpdateConfig_Request_<ContainerAllocator> *;
  using ConstRawPtr =
    const robot_param_atomic::srv::UpdateConfig_Request_<ContainerAllocator> *;
  using SharedPtr =
    std::shared_ptr<robot_param_atomic::srv::UpdateConfig_Request_<ContainerAllocator>>;
  using ConstSharedPtr =
    std::shared_ptr<robot_param_atomic::srv::UpdateConfig_Request_<ContainerAllocator> const>;

  template<typename Deleter = std::default_delete<
      robot_param_atomic::srv::UpdateConfig_Request_<ContainerAllocator>>>
  using UniquePtrWithDeleter =
    std::unique_ptr<robot_param_atomic::srv::UpdateConfig_Request_<ContainerAllocator>, Deleter>;

  using UniquePtr = UniquePtrWithDeleter<>;

  template<typename Deleter = std::default_delete<
      robot_param_atomic::srv::UpdateConfig_Request_<ContainerAllocator>>>
  using ConstUniquePtrWithDeleter =
    std::unique_ptr<robot_param_atomic::srv::UpdateConfig_Request_<ContainerAllocator> const, Deleter>;
  using ConstUniquePtr = ConstUniquePtrWithDeleter<>;

  using WeakPtr =
    std::weak_ptr<robot_param_atomic::srv::UpdateConfig_Request_<ContainerAllocator>>;
  using ConstWeakPtr =
    std::weak_ptr<robot_param_atomic::srv::UpdateConfig_Request_<ContainerAllocator> const>;

  // pointer types similar to ROS 1, use SharedPtr / ConstSharedPtr instead
  // NOTE: Can't use 'using' here because GNU C++ can't parse attributes properly
  typedef DEPRECATED__robot_param_atomic__srv__UpdateConfig_Request
    std::shared_ptr<robot_param_atomic::srv::UpdateConfig_Request_<ContainerAllocator>>
    Ptr;
  typedef DEPRECATED__robot_param_atomic__srv__UpdateConfig_Request
    std::shared_ptr<robot_param_atomic::srv::UpdateConfig_Request_<ContainerAllocator> const>
    ConstPtr;

  // comparison operators
  bool operator==(const UpdateConfig_Request_ & other) const
  {
    if (this->expected_version != other.expected_version) {
      return false;
    }
    if (this->sampling_rate_hz != other.sampling_rate_hz) {
      return false;
    }
    if (this->cache_length_s != other.cache_length_s) {
      return false;
    }
    if (this->allowed_latency_s != other.allowed_latency_s) {
      return false;
    }
    return true;
  }
  bool operator!=(const UpdateConfig_Request_ & other) const
  {
    return !this->operator==(other);
  }
};  // struct UpdateConfig_Request_

// alias to use template instance with default allocator
using UpdateConfig_Request =
  robot_param_atomic::srv::UpdateConfig_Request_<std::allocator<void>>;

// constant definitions

}  // namespace srv

}  // namespace robot_param_atomic


#ifndef _WIN32
# define DEPRECATED__robot_param_atomic__srv__UpdateConfig_Response __attribute__((deprecated))
#else
# define DEPRECATED__robot_param_atomic__srv__UpdateConfig_Response __declspec(deprecated)
#endif

namespace robot_param_atomic
{

namespace srv
{

// message struct
template<class ContainerAllocator>
struct UpdateConfig_Response_
{
  using Type = UpdateConfig_Response_<ContainerAllocator>;

  explicit UpdateConfig_Response_(rosidl_runtime_cpp::MessageInitialization _init = rosidl_runtime_cpp::MessageInitialization::ALL)
  {
    if (rosidl_runtime_cpp::MessageInitialization::ALL == _init ||
      rosidl_runtime_cpp::MessageInitialization::ZERO == _init)
    {
      this->ok = false;
      this->code = 0;
      this->message = "";
      this->version = 0.0;
      this->sampling_rate_hz = 0.0;
      this->cache_length_s = 0.0;
      this->allowed_latency_s = 0.0;
    }
  }

  explicit UpdateConfig_Response_(const ContainerAllocator & _alloc, rosidl_runtime_cpp::MessageInitialization _init = rosidl_runtime_cpp::MessageInitialization::ALL)
  : message(_alloc)
  {
    if (rosidl_runtime_cpp::MessageInitialization::ALL == _init ||
      rosidl_runtime_cpp::MessageInitialization::ZERO == _init)
    {
      this->ok = false;
      this->code = 0;
      this->message = "";
      this->version = 0.0;
      this->sampling_rate_hz = 0.0;
      this->cache_length_s = 0.0;
      this->allowed_latency_s = 0.0;
    }
  }

  // field types and members
  using _ok_type =
    bool;
  _ok_type ok;
  using _code_type =
    uint8_t;
  _code_type code;
  using _message_type =
    std::basic_string<char, std::char_traits<char>, typename std::allocator_traits<ContainerAllocator>::template rebind_alloc<char>>;
  _message_type message;
  using _version_type =
    double;
  _version_type version;
  using _sampling_rate_hz_type =
    double;
  _sampling_rate_hz_type sampling_rate_hz;
  using _cache_length_s_type =
    double;
  _cache_length_s_type cache_length_s;
  using _allowed_latency_s_type =
    double;
  _allowed_latency_s_type allowed_latency_s;

  // setters for named parameter idiom
  Type & set__ok(
    const bool & _arg)
  {
    this->ok = _arg;
    return *this;
  }
  Type & set__code(
    const uint8_t & _arg)
  {
    this->code = _arg;
    return *this;
  }
  Type & set__message(
    const std::basic_string<char, std::char_traits<char>, typename std::allocator_traits<ContainerAllocator>::template rebind_alloc<char>> & _arg)
  {
    this->message = _arg;
    return *this;
  }
  Type & set__version(
    const double & _arg)
  {
    this->version = _arg;
    return *this;
  }
  Type & set__sampling_rate_hz(
    const double & _arg)
  {
    this->sampling_rate_hz = _arg;
    return *this;
  }
  Type & set__cache_length_s(
    const double & _arg)
  {
    this->cache_length_s = _arg;
    return *this;
  }
  Type & set__allowed_latency_s(
    const double & _arg)
  {
    this->allowed_latency_s = _arg;
    return *this;
  }

  // constant declarations
  static constexpr uint8_t OK =
    0u;
  static constexpr uint8_t REJECT_CONSTRAINT =
    1u;
  static constexpr uint8_t REJECT_STALE_VERSION =
    2u;
  static constexpr uint8_t REJECT_UPDATE_IN_CALLBACK =
    3u;
  static constexpr uint8_t ERR_PERSISTENCE =
    4u;
  static constexpr uint8_t ERR_INTERNAL =
    5u;

  // pointer types
  using RawPtr =
    robot_param_atomic::srv::UpdateConfig_Response_<ContainerAllocator> *;
  using ConstRawPtr =
    const robot_param_atomic::srv::UpdateConfig_Response_<ContainerAllocator> *;
  using SharedPtr =
    std::shared_ptr<robot_param_atomic::srv::UpdateConfig_Response_<ContainerAllocator>>;
  using ConstSharedPtr =
    std::shared_ptr<robot_param_atomic::srv::UpdateConfig_Response_<ContainerAllocator> const>;

  template<typename Deleter = std::default_delete<
      robot_param_atomic::srv::UpdateConfig_Response_<ContainerAllocator>>>
  using UniquePtrWithDeleter =
    std::unique_ptr<robot_param_atomic::srv::UpdateConfig_Response_<ContainerAllocator>, Deleter>;

  using UniquePtr = UniquePtrWithDeleter<>;

  template<typename Deleter = std::default_delete<
      robot_param_atomic::srv::UpdateConfig_Response_<ContainerAllocator>>>
  using ConstUniquePtrWithDeleter =
    std::unique_ptr<robot_param_atomic::srv::UpdateConfig_Response_<ContainerAllocator> const, Deleter>;
  using ConstUniquePtr = ConstUniquePtrWithDeleter<>;

  using WeakPtr =
    std::weak_ptr<robot_param_atomic::srv::UpdateConfig_Response_<ContainerAllocator>>;
  using ConstWeakPtr =
    std::weak_ptr<robot_param_atomic::srv::UpdateConfig_Response_<ContainerAllocator> const>;

  // pointer types similar to ROS 1, use SharedPtr / ConstSharedPtr instead
  // NOTE: Can't use 'using' here because GNU C++ can't parse attributes properly
  typedef DEPRECATED__robot_param_atomic__srv__UpdateConfig_Response
    std::shared_ptr<robot_param_atomic::srv::UpdateConfig_Response_<ContainerAllocator>>
    Ptr;
  typedef DEPRECATED__robot_param_atomic__srv__UpdateConfig_Response
    std::shared_ptr<robot_param_atomic::srv::UpdateConfig_Response_<ContainerAllocator> const>
    ConstPtr;

  // comparison operators
  bool operator==(const UpdateConfig_Response_ & other) const
  {
    if (this->ok != other.ok) {
      return false;
    }
    if (this->code != other.code) {
      return false;
    }
    if (this->message != other.message) {
      return false;
    }
    if (this->version != other.version) {
      return false;
    }
    if (this->sampling_rate_hz != other.sampling_rate_hz) {
      return false;
    }
    if (this->cache_length_s != other.cache_length_s) {
      return false;
    }
    if (this->allowed_latency_s != other.allowed_latency_s) {
      return false;
    }
    return true;
  }
  bool operator!=(const UpdateConfig_Response_ & other) const
  {
    return !this->operator==(other);
  }
};  // struct UpdateConfig_Response_

// alias to use template instance with default allocator
using UpdateConfig_Response =
  robot_param_atomic::srv::UpdateConfig_Response_<std::allocator<void>>;

// constant definitions
#if __cplusplus < 201703L
// static constexpr member variable definitions are only needed in C++14 and below, deprecated in C++17
template<typename ContainerAllocator>
constexpr uint8_t UpdateConfig_Response_<ContainerAllocator>::OK;
#endif  // __cplusplus < 201703L
#if __cplusplus < 201703L
// static constexpr member variable definitions are only needed in C++14 and below, deprecated in C++17
template<typename ContainerAllocator>
constexpr uint8_t UpdateConfig_Response_<ContainerAllocator>::REJECT_CONSTRAINT;
#endif  // __cplusplus < 201703L
#if __cplusplus < 201703L
// static constexpr member variable definitions are only needed in C++14 and below, deprecated in C++17
template<typename ContainerAllocator>
constexpr uint8_t UpdateConfig_Response_<ContainerAllocator>::REJECT_STALE_VERSION;
#endif  // __cplusplus < 201703L
#if __cplusplus < 201703L
// static constexpr member variable definitions are only needed in C++14 and below, deprecated in C++17
template<typename ContainerAllocator>
constexpr uint8_t UpdateConfig_Response_<ContainerAllocator>::REJECT_UPDATE_IN_CALLBACK;
#endif  // __cplusplus < 201703L
#if __cplusplus < 201703L
// static constexpr member variable definitions are only needed in C++14 and below, deprecated in C++17
template<typename ContainerAllocator>
constexpr uint8_t UpdateConfig_Response_<ContainerAllocator>::ERR_PERSISTENCE;
#endif  // __cplusplus < 201703L
#if __cplusplus < 201703L
// static constexpr member variable definitions are only needed in C++14 and below, deprecated in C++17
template<typename ContainerAllocator>
constexpr uint8_t UpdateConfig_Response_<ContainerAllocator>::ERR_INTERNAL;
#endif  // __cplusplus < 201703L

}  // namespace srv

}  // namespace robot_param_atomic


// Include directives for member types
// Member 'info'
#include "service_msgs/msg/detail/service_event_info__struct.hpp"

#ifndef _WIN32
# define DEPRECATED__robot_param_atomic__srv__UpdateConfig_Event __attribute__((deprecated))
#else
# define DEPRECATED__robot_param_atomic__srv__UpdateConfig_Event __declspec(deprecated)
#endif

namespace robot_param_atomic
{

namespace srv
{

// message struct
template<class ContainerAllocator>
struct UpdateConfig_Event_
{
  using Type = UpdateConfig_Event_<ContainerAllocator>;

  explicit UpdateConfig_Event_(rosidl_runtime_cpp::MessageInitialization _init = rosidl_runtime_cpp::MessageInitialization::ALL)
  : info(_init)
  {
    (void)_init;
  }

  explicit UpdateConfig_Event_(const ContainerAllocator & _alloc, rosidl_runtime_cpp::MessageInitialization _init = rosidl_runtime_cpp::MessageInitialization::ALL)
  : info(_alloc, _init)
  {
    (void)_init;
  }

  // field types and members
  using _info_type =
    service_msgs::msg::ServiceEventInfo_<ContainerAllocator>;
  _info_type info;
  using _request_type =
    rosidl_runtime_cpp::BoundedVector<robot_param_atomic::srv::UpdateConfig_Request_<ContainerAllocator>, 1, typename std::allocator_traits<ContainerAllocator>::template rebind_alloc<robot_param_atomic::srv::UpdateConfig_Request_<ContainerAllocator>>>;
  _request_type request;
  using _response_type =
    rosidl_runtime_cpp::BoundedVector<robot_param_atomic::srv::UpdateConfig_Response_<ContainerAllocator>, 1, typename std::allocator_traits<ContainerAllocator>::template rebind_alloc<robot_param_atomic::srv::UpdateConfig_Response_<ContainerAllocator>>>;
  _response_type response;

  // setters for named parameter idiom
  Type & set__info(
    const service_msgs::msg::ServiceEventInfo_<ContainerAllocator> & _arg)
  {
    this->info = _arg;
    return *this;
  }
  Type & set__request(
    const rosidl_runtime_cpp::BoundedVector<robot_param_atomic::srv::UpdateConfig_Request_<ContainerAllocator>, 1, typename std::allocator_traits<ContainerAllocator>::template rebind_alloc<robot_param_atomic::srv::UpdateConfig_Request_<ContainerAllocator>>> & _arg)
  {
    this->request = _arg;
    return *this;
  }
  Type & set__response(
    const rosidl_runtime_cpp::BoundedVector<robot_param_atomic::srv::UpdateConfig_Response_<ContainerAllocator>, 1, typename std::allocator_traits<ContainerAllocator>::template rebind_alloc<robot_param_atomic::srv::UpdateConfig_Response_<ContainerAllocator>>> & _arg)
  {
    this->response = _arg;
    return *this;
  }

  // constant declarations

  // pointer types
  using RawPtr =
    robot_param_atomic::srv::UpdateConfig_Event_<ContainerAllocator> *;
  using ConstRawPtr =
    const robot_param_atomic::srv::UpdateConfig_Event_<ContainerAllocator> *;
  using SharedPtr =
    std::shared_ptr<robot_param_atomic::srv::UpdateConfig_Event_<ContainerAllocator>>;
  using ConstSharedPtr =
    std::shared_ptr<robot_param_atomic::srv::UpdateConfig_Event_<ContainerAllocator> const>;

  template<typename Deleter = std::default_delete<
      robot_param_atomic::srv::UpdateConfig_Event_<ContainerAllocator>>>
  using UniquePtrWithDeleter =
    std::unique_ptr<robot_param_atomic::srv::UpdateConfig_Event_<ContainerAllocator>, Deleter>;

  using UniquePtr = UniquePtrWithDeleter<>;

  template<typename Deleter = std::default_delete<
      robot_param_atomic::srv::UpdateConfig_Event_<ContainerAllocator>>>
  using ConstUniquePtrWithDeleter =
    std::unique_ptr<robot_param_atomic::srv::UpdateConfig_Event_<ContainerAllocator> const, Deleter>;
  using ConstUniquePtr = ConstUniquePtrWithDeleter<>;

  using WeakPtr =
    std::weak_ptr<robot_param_atomic::srv::UpdateConfig_Event_<ContainerAllocator>>;
  using ConstWeakPtr =
    std::weak_ptr<robot_param_atomic::srv::UpdateConfig_Event_<ContainerAllocator> const>;

  // pointer types similar to ROS 1, use SharedPtr / ConstSharedPtr instead
  // NOTE: Can't use 'using' here because GNU C++ can't parse attributes properly
  typedef DEPRECATED__robot_param_atomic__srv__UpdateConfig_Event
    std::shared_ptr<robot_param_atomic::srv::UpdateConfig_Event_<ContainerAllocator>>
    Ptr;
  typedef DEPRECATED__robot_param_atomic__srv__UpdateConfig_Event
    std::shared_ptr<robot_param_atomic::srv::UpdateConfig_Event_<ContainerAllocator> const>
    ConstPtr;

  // comparison operators
  bool operator==(const UpdateConfig_Event_ & other) const
  {
    if (this->info != other.info) {
      return false;
    }
    if (this->request != other.request) {
      return false;
    }
    if (this->response != other.response) {
      return false;
    }
    return true;
  }
  bool operator!=(const UpdateConfig_Event_ & other) const
  {
    return !this->operator==(other);
  }
};  // struct UpdateConfig_Event_

// alias to use template instance with default allocator
using UpdateConfig_Event =
  robot_param_atomic::srv::UpdateConfig_Event_<std::allocator<void>>;

// constant definitions

}  // namespace srv

}  // namespace robot_param_atomic

namespace robot_param_atomic
{

namespace srv
{

struct UpdateConfig
{
  using Request = robot_param_atomic::srv::UpdateConfig_Request;
  using Response = robot_param_atomic::srv::UpdateConfig_Response;
  using Event = robot_param_atomic::srv::UpdateConfig_Event;
};

}  // namespace srv

}  // namespace robot_param_atomic

#endif  // ROBOT_PARAM_ATOMIC__SRV__DETAIL__UPDATE_CONFIG__STRUCT_HPP_
