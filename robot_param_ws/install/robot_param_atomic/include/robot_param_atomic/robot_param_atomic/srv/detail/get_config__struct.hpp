// generated from rosidl_generator_cpp/resource/idl__struct.hpp.em
// with input from robot_param_atomic:srv/GetConfig.idl
// generated code does not contain a copyright notice

// IWYU pragma: private, include "robot_param_atomic/srv/get_config.hpp"


#ifndef ROBOT_PARAM_ATOMIC__SRV__DETAIL__GET_CONFIG__STRUCT_HPP_
#define ROBOT_PARAM_ATOMIC__SRV__DETAIL__GET_CONFIG__STRUCT_HPP_

#include <algorithm>
#include <array>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "rosidl_runtime_cpp/bounded_vector.hpp"
#include "rosidl_runtime_cpp/message_initialization.hpp"


#ifndef _WIN32
# define DEPRECATED__robot_param_atomic__srv__GetConfig_Request __attribute__((deprecated))
#else
# define DEPRECATED__robot_param_atomic__srv__GetConfig_Request __declspec(deprecated)
#endif

namespace robot_param_atomic
{

namespace srv
{

// message struct
template<class ContainerAllocator>
struct GetConfig_Request_
{
  using Type = GetConfig_Request_<ContainerAllocator>;

  explicit GetConfig_Request_(rosidl_runtime_cpp::MessageInitialization _init = rosidl_runtime_cpp::MessageInitialization::ALL)
  {
    if (rosidl_runtime_cpp::MessageInitialization::ALL == _init ||
      rosidl_runtime_cpp::MessageInitialization::ZERO == _init)
    {
      this->structure_needs_at_least_one_member = 0;
    }
  }

  explicit GetConfig_Request_(const ContainerAllocator & _alloc, rosidl_runtime_cpp::MessageInitialization _init = rosidl_runtime_cpp::MessageInitialization::ALL)
  {
    (void)_alloc;
    if (rosidl_runtime_cpp::MessageInitialization::ALL == _init ||
      rosidl_runtime_cpp::MessageInitialization::ZERO == _init)
    {
      this->structure_needs_at_least_one_member = 0;
    }
  }

  // field types and members
  using _structure_needs_at_least_one_member_type =
    uint8_t;
  _structure_needs_at_least_one_member_type structure_needs_at_least_one_member;


  // constant declarations

  // pointer types
  using RawPtr =
    robot_param_atomic::srv::GetConfig_Request_<ContainerAllocator> *;
  using ConstRawPtr =
    const robot_param_atomic::srv::GetConfig_Request_<ContainerAllocator> *;
  using SharedPtr =
    std::shared_ptr<robot_param_atomic::srv::GetConfig_Request_<ContainerAllocator>>;
  using ConstSharedPtr =
    std::shared_ptr<robot_param_atomic::srv::GetConfig_Request_<ContainerAllocator> const>;

  template<typename Deleter = std::default_delete<
      robot_param_atomic::srv::GetConfig_Request_<ContainerAllocator>>>
  using UniquePtrWithDeleter =
    std::unique_ptr<robot_param_atomic::srv::GetConfig_Request_<ContainerAllocator>, Deleter>;

  using UniquePtr = UniquePtrWithDeleter<>;

  template<typename Deleter = std::default_delete<
      robot_param_atomic::srv::GetConfig_Request_<ContainerAllocator>>>
  using ConstUniquePtrWithDeleter =
    std::unique_ptr<robot_param_atomic::srv::GetConfig_Request_<ContainerAllocator> const, Deleter>;
  using ConstUniquePtr = ConstUniquePtrWithDeleter<>;

  using WeakPtr =
    std::weak_ptr<robot_param_atomic::srv::GetConfig_Request_<ContainerAllocator>>;
  using ConstWeakPtr =
    std::weak_ptr<robot_param_atomic::srv::GetConfig_Request_<ContainerAllocator> const>;

  // pointer types similar to ROS 1, use SharedPtr / ConstSharedPtr instead
  // NOTE: Can't use 'using' here because GNU C++ can't parse attributes properly
  typedef DEPRECATED__robot_param_atomic__srv__GetConfig_Request
    std::shared_ptr<robot_param_atomic::srv::GetConfig_Request_<ContainerAllocator>>
    Ptr;
  typedef DEPRECATED__robot_param_atomic__srv__GetConfig_Request
    std::shared_ptr<robot_param_atomic::srv::GetConfig_Request_<ContainerAllocator> const>
    ConstPtr;

  // comparison operators
  bool operator==(const GetConfig_Request_ & other) const
  {
    if (this->structure_needs_at_least_one_member != other.structure_needs_at_least_one_member) {
      return false;
    }
    return true;
  }
  bool operator!=(const GetConfig_Request_ & other) const
  {
    return !this->operator==(other);
  }
};  // struct GetConfig_Request_

// alias to use template instance with default allocator
using GetConfig_Request =
  robot_param_atomic::srv::GetConfig_Request_<std::allocator<void>>;

// constant definitions

}  // namespace srv

}  // namespace robot_param_atomic


#ifndef _WIN32
# define DEPRECATED__robot_param_atomic__srv__GetConfig_Response __attribute__((deprecated))
#else
# define DEPRECATED__robot_param_atomic__srv__GetConfig_Response __declspec(deprecated)
#endif

namespace robot_param_atomic
{

namespace srv
{

// message struct
template<class ContainerAllocator>
struct GetConfig_Response_
{
  using Type = GetConfig_Response_<ContainerAllocator>;

  explicit GetConfig_Response_(rosidl_runtime_cpp::MessageInitialization _init = rosidl_runtime_cpp::MessageInitialization::ALL)
  {
    if (rosidl_runtime_cpp::MessageInitialization::ALL == _init ||
      rosidl_runtime_cpp::MessageInitialization::ZERO == _init)
    {
      this->version = 0.0;
      this->sampling_rate_hz = 0.0;
      this->cache_length_s = 0.0;
      this->allowed_latency_s = 0.0;
      this->config_hash = "";
      this->commit_seq = 0ll;
    }
  }

  explicit GetConfig_Response_(const ContainerAllocator & _alloc, rosidl_runtime_cpp::MessageInitialization _init = rosidl_runtime_cpp::MessageInitialization::ALL)
  : config_hash(_alloc)
  {
    if (rosidl_runtime_cpp::MessageInitialization::ALL == _init ||
      rosidl_runtime_cpp::MessageInitialization::ZERO == _init)
    {
      this->version = 0.0;
      this->sampling_rate_hz = 0.0;
      this->cache_length_s = 0.0;
      this->allowed_latency_s = 0.0;
      this->config_hash = "";
      this->commit_seq = 0ll;
    }
  }

  // field types and members
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
  using _config_hash_type =
    std::basic_string<char, std::char_traits<char>, typename std::allocator_traits<ContainerAllocator>::template rebind_alloc<char>>;
  _config_hash_type config_hash;
  using _commit_seq_type =
    int64_t;
  _commit_seq_type commit_seq;

  // setters for named parameter idiom
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
  Type & set__config_hash(
    const std::basic_string<char, std::char_traits<char>, typename std::allocator_traits<ContainerAllocator>::template rebind_alloc<char>> & _arg)
  {
    this->config_hash = _arg;
    return *this;
  }
  Type & set__commit_seq(
    const int64_t & _arg)
  {
    this->commit_seq = _arg;
    return *this;
  }

  // constant declarations

  // pointer types
  using RawPtr =
    robot_param_atomic::srv::GetConfig_Response_<ContainerAllocator> *;
  using ConstRawPtr =
    const robot_param_atomic::srv::GetConfig_Response_<ContainerAllocator> *;
  using SharedPtr =
    std::shared_ptr<robot_param_atomic::srv::GetConfig_Response_<ContainerAllocator>>;
  using ConstSharedPtr =
    std::shared_ptr<robot_param_atomic::srv::GetConfig_Response_<ContainerAllocator> const>;

  template<typename Deleter = std::default_delete<
      robot_param_atomic::srv::GetConfig_Response_<ContainerAllocator>>>
  using UniquePtrWithDeleter =
    std::unique_ptr<robot_param_atomic::srv::GetConfig_Response_<ContainerAllocator>, Deleter>;

  using UniquePtr = UniquePtrWithDeleter<>;

  template<typename Deleter = std::default_delete<
      robot_param_atomic::srv::GetConfig_Response_<ContainerAllocator>>>
  using ConstUniquePtrWithDeleter =
    std::unique_ptr<robot_param_atomic::srv::GetConfig_Response_<ContainerAllocator> const, Deleter>;
  using ConstUniquePtr = ConstUniquePtrWithDeleter<>;

  using WeakPtr =
    std::weak_ptr<robot_param_atomic::srv::GetConfig_Response_<ContainerAllocator>>;
  using ConstWeakPtr =
    std::weak_ptr<robot_param_atomic::srv::GetConfig_Response_<ContainerAllocator> const>;

  // pointer types similar to ROS 1, use SharedPtr / ConstSharedPtr instead
  // NOTE: Can't use 'using' here because GNU C++ can't parse attributes properly
  typedef DEPRECATED__robot_param_atomic__srv__GetConfig_Response
    std::shared_ptr<robot_param_atomic::srv::GetConfig_Response_<ContainerAllocator>>
    Ptr;
  typedef DEPRECATED__robot_param_atomic__srv__GetConfig_Response
    std::shared_ptr<robot_param_atomic::srv::GetConfig_Response_<ContainerAllocator> const>
    ConstPtr;

  // comparison operators
  bool operator==(const GetConfig_Response_ & other) const
  {
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
    if (this->config_hash != other.config_hash) {
      return false;
    }
    if (this->commit_seq != other.commit_seq) {
      return false;
    }
    return true;
  }
  bool operator!=(const GetConfig_Response_ & other) const
  {
    return !this->operator==(other);
  }
};  // struct GetConfig_Response_

// alias to use template instance with default allocator
using GetConfig_Response =
  robot_param_atomic::srv::GetConfig_Response_<std::allocator<void>>;

// constant definitions

}  // namespace srv

}  // namespace robot_param_atomic


// Include directives for member types
// Member 'info'
#include "service_msgs/msg/detail/service_event_info__struct.hpp"

#ifndef _WIN32
# define DEPRECATED__robot_param_atomic__srv__GetConfig_Event __attribute__((deprecated))
#else
# define DEPRECATED__robot_param_atomic__srv__GetConfig_Event __declspec(deprecated)
#endif

namespace robot_param_atomic
{

namespace srv
{

// message struct
template<class ContainerAllocator>
struct GetConfig_Event_
{
  using Type = GetConfig_Event_<ContainerAllocator>;

  explicit GetConfig_Event_(rosidl_runtime_cpp::MessageInitialization _init = rosidl_runtime_cpp::MessageInitialization::ALL)
  : info(_init)
  {
    (void)_init;
  }

  explicit GetConfig_Event_(const ContainerAllocator & _alloc, rosidl_runtime_cpp::MessageInitialization _init = rosidl_runtime_cpp::MessageInitialization::ALL)
  : info(_alloc, _init)
  {
    (void)_init;
  }

  // field types and members
  using _info_type =
    service_msgs::msg::ServiceEventInfo_<ContainerAllocator>;
  _info_type info;
  using _request_type =
    rosidl_runtime_cpp::BoundedVector<robot_param_atomic::srv::GetConfig_Request_<ContainerAllocator>, 1, typename std::allocator_traits<ContainerAllocator>::template rebind_alloc<robot_param_atomic::srv::GetConfig_Request_<ContainerAllocator>>>;
  _request_type request;
  using _response_type =
    rosidl_runtime_cpp::BoundedVector<robot_param_atomic::srv::GetConfig_Response_<ContainerAllocator>, 1, typename std::allocator_traits<ContainerAllocator>::template rebind_alloc<robot_param_atomic::srv::GetConfig_Response_<ContainerAllocator>>>;
  _response_type response;

  // setters for named parameter idiom
  Type & set__info(
    const service_msgs::msg::ServiceEventInfo_<ContainerAllocator> & _arg)
  {
    this->info = _arg;
    return *this;
  }
  Type & set__request(
    const rosidl_runtime_cpp::BoundedVector<robot_param_atomic::srv::GetConfig_Request_<ContainerAllocator>, 1, typename std::allocator_traits<ContainerAllocator>::template rebind_alloc<robot_param_atomic::srv::GetConfig_Request_<ContainerAllocator>>> & _arg)
  {
    this->request = _arg;
    return *this;
  }
  Type & set__response(
    const rosidl_runtime_cpp::BoundedVector<robot_param_atomic::srv::GetConfig_Response_<ContainerAllocator>, 1, typename std::allocator_traits<ContainerAllocator>::template rebind_alloc<robot_param_atomic::srv::GetConfig_Response_<ContainerAllocator>>> & _arg)
  {
    this->response = _arg;
    return *this;
  }

  // constant declarations

  // pointer types
  using RawPtr =
    robot_param_atomic::srv::GetConfig_Event_<ContainerAllocator> *;
  using ConstRawPtr =
    const robot_param_atomic::srv::GetConfig_Event_<ContainerAllocator> *;
  using SharedPtr =
    std::shared_ptr<robot_param_atomic::srv::GetConfig_Event_<ContainerAllocator>>;
  using ConstSharedPtr =
    std::shared_ptr<robot_param_atomic::srv::GetConfig_Event_<ContainerAllocator> const>;

  template<typename Deleter = std::default_delete<
      robot_param_atomic::srv::GetConfig_Event_<ContainerAllocator>>>
  using UniquePtrWithDeleter =
    std::unique_ptr<robot_param_atomic::srv::GetConfig_Event_<ContainerAllocator>, Deleter>;

  using UniquePtr = UniquePtrWithDeleter<>;

  template<typename Deleter = std::default_delete<
      robot_param_atomic::srv::GetConfig_Event_<ContainerAllocator>>>
  using ConstUniquePtrWithDeleter =
    std::unique_ptr<robot_param_atomic::srv::GetConfig_Event_<ContainerAllocator> const, Deleter>;
  using ConstUniquePtr = ConstUniquePtrWithDeleter<>;

  using WeakPtr =
    std::weak_ptr<robot_param_atomic::srv::GetConfig_Event_<ContainerAllocator>>;
  using ConstWeakPtr =
    std::weak_ptr<robot_param_atomic::srv::GetConfig_Event_<ContainerAllocator> const>;

  // pointer types similar to ROS 1, use SharedPtr / ConstSharedPtr instead
  // NOTE: Can't use 'using' here because GNU C++ can't parse attributes properly
  typedef DEPRECATED__robot_param_atomic__srv__GetConfig_Event
    std::shared_ptr<robot_param_atomic::srv::GetConfig_Event_<ContainerAllocator>>
    Ptr;
  typedef DEPRECATED__robot_param_atomic__srv__GetConfig_Event
    std::shared_ptr<robot_param_atomic::srv::GetConfig_Event_<ContainerAllocator> const>
    ConstPtr;

  // comparison operators
  bool operator==(const GetConfig_Event_ & other) const
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
  bool operator!=(const GetConfig_Event_ & other) const
  {
    return !this->operator==(other);
  }
};  // struct GetConfig_Event_

// alias to use template instance with default allocator
using GetConfig_Event =
  robot_param_atomic::srv::GetConfig_Event_<std::allocator<void>>;

// constant definitions

}  // namespace srv

}  // namespace robot_param_atomic

namespace robot_param_atomic
{

namespace srv
{

struct GetConfig
{
  using Request = robot_param_atomic::srv::GetConfig_Request;
  using Response = robot_param_atomic::srv::GetConfig_Response;
  using Event = robot_param_atomic::srv::GetConfig_Event;
};

}  // namespace srv

}  // namespace robot_param_atomic

#endif  // ROBOT_PARAM_ATOMIC__SRV__DETAIL__GET_CONFIG__STRUCT_HPP_
