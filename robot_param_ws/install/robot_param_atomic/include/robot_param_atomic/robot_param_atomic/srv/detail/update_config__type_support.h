// generated from rosidl_generator_c/resource/idl__type_support.h.em
// with input from robot_param_atomic:srv/UpdateConfig.idl
// generated code does not contain a copyright notice

// IWYU pragma: private, include "robot_param_atomic/srv/update_config.h"


#ifndef ROBOT_PARAM_ATOMIC__SRV__DETAIL__UPDATE_CONFIG__TYPE_SUPPORT_H_
#define ROBOT_PARAM_ATOMIC__SRV__DETAIL__UPDATE_CONFIG__TYPE_SUPPORT_H_

#include "rosidl_typesupport_interface/macros.h"

#include "robot_param_atomic/msg/rosidl_generator_c__visibility_control.h"

#ifdef __cplusplus
extern "C"
{
#endif

#include "rosidl_runtime_c/message_type_support_struct.h"

// Forward declare the get type support functions for this type.
ROSIDL_GENERATOR_C_PUBLIC_robot_param_atomic
const rosidl_message_type_support_t *
ROSIDL_TYPESUPPORT_INTERFACE__MESSAGE_SYMBOL_NAME(
  rosidl_typesupport_c,
  robot_param_atomic,
  srv,
  UpdateConfig_Request
)(void);

// already included above
// #include "rosidl_runtime_c/message_type_support_struct.h"

// Forward declare the get type support functions for this type.
ROSIDL_GENERATOR_C_PUBLIC_robot_param_atomic
const rosidl_message_type_support_t *
ROSIDL_TYPESUPPORT_INTERFACE__MESSAGE_SYMBOL_NAME(
  rosidl_typesupport_c,
  robot_param_atomic,
  srv,
  UpdateConfig_Response
)(void);

// already included above
// #include "rosidl_runtime_c/message_type_support_struct.h"

// Forward declare the get type support functions for this type.
ROSIDL_GENERATOR_C_PUBLIC_robot_param_atomic
const rosidl_message_type_support_t *
ROSIDL_TYPESUPPORT_INTERFACE__MESSAGE_SYMBOL_NAME(
  rosidl_typesupport_c,
  robot_param_atomic,
  srv,
  UpdateConfig_Event
)(void);

#include "rosidl_runtime_c/service_type_support_struct.h"

// Forward declare the get type support functions for this type.
ROSIDL_GENERATOR_C_PUBLIC_robot_param_atomic
const rosidl_service_type_support_t *
ROSIDL_TYPESUPPORT_INTERFACE__SERVICE_SYMBOL_NAME(
  rosidl_typesupport_c,
  robot_param_atomic,
  srv,
  UpdateConfig
)(void);

// Forward declare the function to create a service event message for this type.
ROSIDL_GENERATOR_C_PUBLIC_robot_param_atomic
void *
ROSIDL_TYPESUPPORT_INTERFACE__SERVICE_CREATE_EVENT_MESSAGE_SYMBOL_NAME(
  rosidl_typesupport_c,
  robot_param_atomic,
  srv,
  UpdateConfig
)(
  const rosidl_service_introspection_info_t * info,
  rcutils_allocator_t * allocator,
  const void * request_message,
  const void * response_message);

// Forward declare the function to destroy a service event message for this type.
ROSIDL_GENERATOR_C_PUBLIC_robot_param_atomic
bool
ROSIDL_TYPESUPPORT_INTERFACE__SERVICE_DESTROY_EVENT_MESSAGE_SYMBOL_NAME(
  rosidl_typesupport_c,
  robot_param_atomic,
  srv,
  UpdateConfig
)(
  void * event_msg,
  rcutils_allocator_t * allocator);

#ifdef __cplusplus
}
#endif

#endif  // ROBOT_PARAM_ATOMIC__SRV__DETAIL__UPDATE_CONFIG__TYPE_SUPPORT_H_
