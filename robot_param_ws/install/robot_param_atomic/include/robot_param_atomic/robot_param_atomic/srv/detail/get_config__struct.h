// generated from rosidl_generator_c/resource/idl__struct.h.em
// with input from robot_param_atomic:srv/GetConfig.idl
// generated code does not contain a copyright notice

// IWYU pragma: private, include "robot_param_atomic/srv/get_config.h"


#ifndef ROBOT_PARAM_ATOMIC__SRV__DETAIL__GET_CONFIG__STRUCT_H_
#define ROBOT_PARAM_ATOMIC__SRV__DETAIL__GET_CONFIG__STRUCT_H_

#ifdef __cplusplus
extern "C"
{
#endif

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>


// Constants defined in the message

/// Struct defined in srv/GetConfig in the package robot_param_atomic.
typedef struct robot_param_atomic__srv__GetConfig_Request
{
  uint8_t structure_needs_at_least_one_member;
} robot_param_atomic__srv__GetConfig_Request;

// Struct for a sequence of robot_param_atomic__srv__GetConfig_Request.
typedef struct robot_param_atomic__srv__GetConfig_Request__Sequence
{
  robot_param_atomic__srv__GetConfig_Request * data;
  /// The number of valid items in data
  size_t size;
  /// The number of allocated items in data
  size_t capacity;
} robot_param_atomic__srv__GetConfig_Request__Sequence;

// Constants defined in the message

// Include directives for member types
// Member 'config_hash'
#include "rosidl_runtime_c/string.h"

/// Struct defined in srv/GetConfig in the package robot_param_atomic.
typedef struct robot_param_atomic__srv__GetConfig_Response
{
  double version;
  double sampling_rate_hz;
  double cache_length_s;
  double allowed_latency_s;
  /// SHA-256 hex digest of the canonical JSON snapshot that was persisted.
  rosidl_runtime_c__String config_hash;
  /// Monotonic sequence incremented for every successfully committed update.
  int64_t commit_seq;
} robot_param_atomic__srv__GetConfig_Response;

// Struct for a sequence of robot_param_atomic__srv__GetConfig_Response.
typedef struct robot_param_atomic__srv__GetConfig_Response__Sequence
{
  robot_param_atomic__srv__GetConfig_Response * data;
  /// The number of valid items in data
  size_t size;
  /// The number of allocated items in data
  size_t capacity;
} robot_param_atomic__srv__GetConfig_Response__Sequence;

// Constants defined in the message

// Include directives for member types
// Member 'info'
#include "service_msgs/msg/detail/service_event_info__struct.h"

// constants for array fields with an upper bound
// request
enum
{
  robot_param_atomic__srv__GetConfig_Event__request__MAX_SIZE = 1
};
// response
enum
{
  robot_param_atomic__srv__GetConfig_Event__response__MAX_SIZE = 1
};

/// Struct defined in srv/GetConfig in the package robot_param_atomic.
typedef struct robot_param_atomic__srv__GetConfig_Event
{
  service_msgs__msg__ServiceEventInfo info;
  robot_param_atomic__srv__GetConfig_Request__Sequence request;
  robot_param_atomic__srv__GetConfig_Response__Sequence response;
} robot_param_atomic__srv__GetConfig_Event;

// Struct for a sequence of robot_param_atomic__srv__GetConfig_Event.
typedef struct robot_param_atomic__srv__GetConfig_Event__Sequence
{
  robot_param_atomic__srv__GetConfig_Event * data;
  /// The number of valid items in data
  size_t size;
  /// The number of allocated items in data
  size_t capacity;
} robot_param_atomic__srv__GetConfig_Event__Sequence;

#ifdef __cplusplus
}
#endif

#endif  // ROBOT_PARAM_ATOMIC__SRV__DETAIL__GET_CONFIG__STRUCT_H_
