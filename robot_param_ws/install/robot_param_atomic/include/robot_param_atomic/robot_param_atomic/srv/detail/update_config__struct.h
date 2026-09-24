// generated from rosidl_generator_c/resource/idl__struct.h.em
// with input from robot_param_atomic:srv/UpdateConfig.idl
// generated code does not contain a copyright notice

// IWYU pragma: private, include "robot_param_atomic/srv/update_config.h"


#ifndef ROBOT_PARAM_ATOMIC__SRV__DETAIL__UPDATE_CONFIG__STRUCT_H_
#define ROBOT_PARAM_ATOMIC__SRV__DETAIL__UPDATE_CONFIG__STRUCT_H_

#ifdef __cplusplus
extern "C"
{
#endif

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>


// Constants defined in the message

/// Struct defined in srv/UpdateConfig in the package robot_param_atomic.
typedef struct robot_param_atomic__srv__UpdateConfig_Request
{
  double expected_version;
  double sampling_rate_hz;
  double cache_length_s;
  double allowed_latency_s;
} robot_param_atomic__srv__UpdateConfig_Request;

// Struct for a sequence of robot_param_atomic__srv__UpdateConfig_Request.
typedef struct robot_param_atomic__srv__UpdateConfig_Request__Sequence
{
  robot_param_atomic__srv__UpdateConfig_Request * data;
  /// The number of valid items in data
  size_t size;
  /// The number of allocated items in data
  size_t capacity;
} robot_param_atomic__srv__UpdateConfig_Request__Sequence;

// Constants defined in the message

/// Constant 'OK'.
enum
{
  robot_param_atomic__srv__UpdateConfig_Response__OK = 0
};

/// Constant 'REJECT_CONSTRAINT'.
/**
  * per-field or cross-field validation failed
 */
enum
{
  robot_param_atomic__srv__UpdateConfig_Response__REJECT_CONSTRAINT = 1
};

/// Constant 'REJECT_STALE_VERSION'.
/**
  * expected_version != current version
 */
enum
{
  robot_param_atomic__srv__UpdateConfig_Response__REJECT_STALE_VERSION = 2
};

/// Constant 'REJECT_UPDATE_IN_CALLBACK'.
/**
  * commit attempted from inside a change callback
 */
enum
{
  robot_param_atomic__srv__UpdateConfig_Response__REJECT_UPDATE_IN_CALLBACK = 3
};

/// Constant 'ERR_PERSISTENCE'.
/**
  * SQLite write failed / rolled back
 */
enum
{
  robot_param_atomic__srv__UpdateConfig_Response__ERR_PERSISTENCE = 4
};

/// Constant 'ERR_INTERNAL'.
enum
{
  robot_param_atomic__srv__UpdateConfig_Response__ERR_INTERNAL = 5
};

// Include directives for member types
// Member 'message'
#include "rosidl_runtime_c/string.h"

/// Struct defined in srv/UpdateConfig in the package robot_param_atomic.
typedef struct robot_param_atomic__srv__UpdateConfig_Response
{
  bool ok;
  uint8_t code;
  rosidl_runtime_c__String message;
  /// Committed snapshot after the attempt (unchanged on rejection).
  double version;
  double sampling_rate_hz;
  double cache_length_s;
  double allowed_latency_s;
} robot_param_atomic__srv__UpdateConfig_Response;

// Struct for a sequence of robot_param_atomic__srv__UpdateConfig_Response.
typedef struct robot_param_atomic__srv__UpdateConfig_Response__Sequence
{
  robot_param_atomic__srv__UpdateConfig_Response * data;
  /// The number of valid items in data
  size_t size;
  /// The number of allocated items in data
  size_t capacity;
} robot_param_atomic__srv__UpdateConfig_Response__Sequence;

// Constants defined in the message

// Include directives for member types
// Member 'info'
#include "service_msgs/msg/detail/service_event_info__struct.h"

// constants for array fields with an upper bound
// request
enum
{
  robot_param_atomic__srv__UpdateConfig_Event__request__MAX_SIZE = 1
};
// response
enum
{
  robot_param_atomic__srv__UpdateConfig_Event__response__MAX_SIZE = 1
};

/// Struct defined in srv/UpdateConfig in the package robot_param_atomic.
typedef struct robot_param_atomic__srv__UpdateConfig_Event
{
  service_msgs__msg__ServiceEventInfo info;
  robot_param_atomic__srv__UpdateConfig_Request__Sequence request;
  robot_param_atomic__srv__UpdateConfig_Response__Sequence response;
} robot_param_atomic__srv__UpdateConfig_Event;

// Struct for a sequence of robot_param_atomic__srv__UpdateConfig_Event.
typedef struct robot_param_atomic__srv__UpdateConfig_Event__Sequence
{
  robot_param_atomic__srv__UpdateConfig_Event * data;
  /// The number of valid items in data
  size_t size;
  /// The number of allocated items in data
  size_t capacity;
} robot_param_atomic__srv__UpdateConfig_Event__Sequence;

#ifdef __cplusplus
}
#endif

#endif  // ROBOT_PARAM_ATOMIC__SRV__DETAIL__UPDATE_CONFIG__STRUCT_H_
