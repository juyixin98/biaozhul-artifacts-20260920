// generated from rosidl_typesupport_introspection_c/resource/idl__type_support.c.em
// with input from robot_param_atomic:srv/UpdateConfig.idl
// generated code does not contain a copyright notice

#include <stddef.h>
#include "robot_param_atomic/srv/detail/update_config__rosidl_typesupport_introspection_c.h"
#include "robot_param_atomic/msg/rosidl_typesupport_introspection_c__visibility_control.h"
#include "rosidl_typesupport_introspection_c/field_types.h"
#include "rosidl_typesupport_introspection_c/identifier.h"
#include "rosidl_typesupport_introspection_c/message_introspection.h"
#include "robot_param_atomic/srv/detail/update_config__functions.h"
#include "robot_param_atomic/srv/detail/update_config__struct.h"


#ifdef __cplusplus
extern "C"
{
#endif

void robot_param_atomic__srv__UpdateConfig_Request__rosidl_typesupport_introspection_c__UpdateConfig_Request_init_function(
  void * message_memory, enum rosidl_runtime_c__message_initialization _init)
{
  // TODO(karsten1987): initializers are not yet implemented for typesupport c
  // see https://github.com/ros2/ros2/issues/397
  (void) _init;
  robot_param_atomic__srv__UpdateConfig_Request__init(message_memory);
}

void robot_param_atomic__srv__UpdateConfig_Request__rosidl_typesupport_introspection_c__UpdateConfig_Request_fini_function(void * message_memory)
{
  robot_param_atomic__srv__UpdateConfig_Request__fini(message_memory);
}

static rosidl_typesupport_introspection_c__MessageMember robot_param_atomic__srv__UpdateConfig_Request__rosidl_typesupport_introspection_c__UpdateConfig_Request_message_member_array[4] = {
  {
    "expected_version",  // name
    rosidl_typesupport_introspection_c__ROS_TYPE_DOUBLE,  // type
    0,  // upper bound of string
    NULL,  // members of sub message
    false,  // is key
    false,  // is array
    0,  // array size
    false,  // is upper bound
    offsetof(robot_param_atomic__srv__UpdateConfig_Request, expected_version),  // bytes offset in struct
    NULL,  // default value
    NULL,  // size() function pointer
    NULL,  // get_const(index) function pointer
    NULL,  // get(index) function pointer
    NULL,  // fetch(index, &value) function pointer
    NULL,  // assign(index, value) function pointer
    NULL  // resize(index) function pointer
  },
  {
    "sampling_rate_hz",  // name
    rosidl_typesupport_introspection_c__ROS_TYPE_DOUBLE,  // type
    0,  // upper bound of string
    NULL,  // members of sub message
    false,  // is key
    false,  // is array
    0,  // array size
    false,  // is upper bound
    offsetof(robot_param_atomic__srv__UpdateConfig_Request, sampling_rate_hz),  // bytes offset in struct
    NULL,  // default value
    NULL,  // size() function pointer
    NULL,  // get_const(index) function pointer
    NULL,  // get(index) function pointer
    NULL,  // fetch(index, &value) function pointer
    NULL,  // assign(index, value) function pointer
    NULL  // resize(index) function pointer
  },
  {
    "cache_length_s",  // name
    rosidl_typesupport_introspection_c__ROS_TYPE_DOUBLE,  // type
    0,  // upper bound of string
    NULL,  // members of sub message
    false,  // is key
    false,  // is array
    0,  // array size
    false,  // is upper bound
    offsetof(robot_param_atomic__srv__UpdateConfig_Request, cache_length_s),  // bytes offset in struct
    NULL,  // default value
    NULL,  // size() function pointer
    NULL,  // get_const(index) function pointer
    NULL,  // get(index) function pointer
    NULL,  // fetch(index, &value) function pointer
    NULL,  // assign(index, value) function pointer
    NULL  // resize(index) function pointer
  },
  {
    "allowed_latency_s",  // name
    rosidl_typesupport_introspection_c__ROS_TYPE_DOUBLE,  // type
    0,  // upper bound of string
    NULL,  // members of sub message
    false,  // is key
    false,  // is array
    0,  // array size
    false,  // is upper bound
    offsetof(robot_param_atomic__srv__UpdateConfig_Request, allowed_latency_s),  // bytes offset in struct
    NULL,  // default value
    NULL,  // size() function pointer
    NULL,  // get_const(index) function pointer
    NULL,  // get(index) function pointer
    NULL,  // fetch(index, &value) function pointer
    NULL,  // assign(index, value) function pointer
    NULL  // resize(index) function pointer
  }
};

static const rosidl_typesupport_introspection_c__MessageMembers robot_param_atomic__srv__UpdateConfig_Request__rosidl_typesupport_introspection_c__UpdateConfig_Request_message_members = {
  "robot_param_atomic__srv",  // message namespace
  "UpdateConfig_Request",  // message name
  4,  // number of fields
  sizeof(robot_param_atomic__srv__UpdateConfig_Request),
  false,  // has_any_key_member_
  robot_param_atomic__srv__UpdateConfig_Request__rosidl_typesupport_introspection_c__UpdateConfig_Request_message_member_array,  // message members
  robot_param_atomic__srv__UpdateConfig_Request__rosidl_typesupport_introspection_c__UpdateConfig_Request_init_function,  // function to initialize message memory (memory has to be allocated)
  robot_param_atomic__srv__UpdateConfig_Request__rosidl_typesupport_introspection_c__UpdateConfig_Request_fini_function  // function to terminate message instance (will not free memory)
};

// this is not const since it must be initialized on first access
// since C does not allow non-integral compile-time constants
static rosidl_message_type_support_t robot_param_atomic__srv__UpdateConfig_Request__rosidl_typesupport_introspection_c__UpdateConfig_Request_message_type_support_handle = {
  0,
  &robot_param_atomic__srv__UpdateConfig_Request__rosidl_typesupport_introspection_c__UpdateConfig_Request_message_members,
  get_message_typesupport_handle_function,
  &robot_param_atomic__srv__UpdateConfig_Request__get_type_hash,
  &robot_param_atomic__srv__UpdateConfig_Request__get_type_description,
  &robot_param_atomic__srv__UpdateConfig_Request__get_type_description_sources,
};

ROSIDL_TYPESUPPORT_INTROSPECTION_C_EXPORT_robot_param_atomic
const rosidl_message_type_support_t *
ROSIDL_TYPESUPPORT_INTERFACE__MESSAGE_SYMBOL_NAME(rosidl_typesupport_introspection_c, robot_param_atomic, srv, UpdateConfig_Request)() {
  if (!robot_param_atomic__srv__UpdateConfig_Request__rosidl_typesupport_introspection_c__UpdateConfig_Request_message_type_support_handle.typesupport_identifier) {
    robot_param_atomic__srv__UpdateConfig_Request__rosidl_typesupport_introspection_c__UpdateConfig_Request_message_type_support_handle.typesupport_identifier =
      rosidl_typesupport_introspection_c__identifier;
  }
  return &robot_param_atomic__srv__UpdateConfig_Request__rosidl_typesupport_introspection_c__UpdateConfig_Request_message_type_support_handle;
}
#ifdef __cplusplus
}
#endif

// already included above
// #include <stddef.h>
// already included above
// #include "robot_param_atomic/srv/detail/update_config__rosidl_typesupport_introspection_c.h"
// already included above
// #include "robot_param_atomic/msg/rosidl_typesupport_introspection_c__visibility_control.h"
// already included above
// #include "rosidl_typesupport_introspection_c/field_types.h"
// already included above
// #include "rosidl_typesupport_introspection_c/identifier.h"
// already included above
// #include "rosidl_typesupport_introspection_c/message_introspection.h"
// already included above
// #include "robot_param_atomic/srv/detail/update_config__functions.h"
// already included above
// #include "robot_param_atomic/srv/detail/update_config__struct.h"


// Include directives for member types
// Member `message`
#include "rosidl_runtime_c/string_functions.h"

#ifdef __cplusplus
extern "C"
{
#endif

void robot_param_atomic__srv__UpdateConfig_Response__rosidl_typesupport_introspection_c__UpdateConfig_Response_init_function(
  void * message_memory, enum rosidl_runtime_c__message_initialization _init)
{
  // TODO(karsten1987): initializers are not yet implemented for typesupport c
  // see https://github.com/ros2/ros2/issues/397
  (void) _init;
  robot_param_atomic__srv__UpdateConfig_Response__init(message_memory);
}

void robot_param_atomic__srv__UpdateConfig_Response__rosidl_typesupport_introspection_c__UpdateConfig_Response_fini_function(void * message_memory)
{
  robot_param_atomic__srv__UpdateConfig_Response__fini(message_memory);
}

static rosidl_typesupport_introspection_c__MessageMember robot_param_atomic__srv__UpdateConfig_Response__rosidl_typesupport_introspection_c__UpdateConfig_Response_message_member_array[7] = {
  {
    "ok",  // name
    rosidl_typesupport_introspection_c__ROS_TYPE_BOOLEAN,  // type
    0,  // upper bound of string
    NULL,  // members of sub message
    false,  // is key
    false,  // is array
    0,  // array size
    false,  // is upper bound
    offsetof(robot_param_atomic__srv__UpdateConfig_Response, ok),  // bytes offset in struct
    NULL,  // default value
    NULL,  // size() function pointer
    NULL,  // get_const(index) function pointer
    NULL,  // get(index) function pointer
    NULL,  // fetch(index, &value) function pointer
    NULL,  // assign(index, value) function pointer
    NULL  // resize(index) function pointer
  },
  {
    "code",  // name
    rosidl_typesupport_introspection_c__ROS_TYPE_UINT8,  // type
    0,  // upper bound of string
    NULL,  // members of sub message
    false,  // is key
    false,  // is array
    0,  // array size
    false,  // is upper bound
    offsetof(robot_param_atomic__srv__UpdateConfig_Response, code),  // bytes offset in struct
    NULL,  // default value
    NULL,  // size() function pointer
    NULL,  // get_const(index) function pointer
    NULL,  // get(index) function pointer
    NULL,  // fetch(index, &value) function pointer
    NULL,  // assign(index, value) function pointer
    NULL  // resize(index) function pointer
  },
  {
    "message",  // name
    rosidl_typesupport_introspection_c__ROS_TYPE_STRING,  // type
    0,  // upper bound of string
    NULL,  // members of sub message
    false,  // is key
    false,  // is array
    0,  // array size
    false,  // is upper bound
    offsetof(robot_param_atomic__srv__UpdateConfig_Response, message),  // bytes offset in struct
    NULL,  // default value
    NULL,  // size() function pointer
    NULL,  // get_const(index) function pointer
    NULL,  // get(index) function pointer
    NULL,  // fetch(index, &value) function pointer
    NULL,  // assign(index, value) function pointer
    NULL  // resize(index) function pointer
  },
  {
    "version",  // name
    rosidl_typesupport_introspection_c__ROS_TYPE_DOUBLE,  // type
    0,  // upper bound of string
    NULL,  // members of sub message
    false,  // is key
    false,  // is array
    0,  // array size
    false,  // is upper bound
    offsetof(robot_param_atomic__srv__UpdateConfig_Response, version),  // bytes offset in struct
    NULL,  // default value
    NULL,  // size() function pointer
    NULL,  // get_const(index) function pointer
    NULL,  // get(index) function pointer
    NULL,  // fetch(index, &value) function pointer
    NULL,  // assign(index, value) function pointer
    NULL  // resize(index) function pointer
  },
  {
    "sampling_rate_hz",  // name
    rosidl_typesupport_introspection_c__ROS_TYPE_DOUBLE,  // type
    0,  // upper bound of string
    NULL,  // members of sub message
    false,  // is key
    false,  // is array
    0,  // array size
    false,  // is upper bound
    offsetof(robot_param_atomic__srv__UpdateConfig_Response, sampling_rate_hz),  // bytes offset in struct
    NULL,  // default value
    NULL,  // size() function pointer
    NULL,  // get_const(index) function pointer
    NULL,  // get(index) function pointer
    NULL,  // fetch(index, &value) function pointer
    NULL,  // assign(index, value) function pointer
    NULL  // resize(index) function pointer
  },
  {
    "cache_length_s",  // name
    rosidl_typesupport_introspection_c__ROS_TYPE_DOUBLE,  // type
    0,  // upper bound of string
    NULL,  // members of sub message
    false,  // is key
    false,  // is array
    0,  // array size
    false,  // is upper bound
    offsetof(robot_param_atomic__srv__UpdateConfig_Response, cache_length_s),  // bytes offset in struct
    NULL,  // default value
    NULL,  // size() function pointer
    NULL,  // get_const(index) function pointer
    NULL,  // get(index) function pointer
    NULL,  // fetch(index, &value) function pointer
    NULL,  // assign(index, value) function pointer
    NULL  // resize(index) function pointer
  },
  {
    "allowed_latency_s",  // name
    rosidl_typesupport_introspection_c__ROS_TYPE_DOUBLE,  // type
    0,  // upper bound of string
    NULL,  // members of sub message
    false,  // is key
    false,  // is array
    0,  // array size
    false,  // is upper bound
    offsetof(robot_param_atomic__srv__UpdateConfig_Response, allowed_latency_s),  // bytes offset in struct
    NULL,  // default value
    NULL,  // size() function pointer
    NULL,  // get_const(index) function pointer
    NULL,  // get(index) function pointer
    NULL,  // fetch(index, &value) function pointer
    NULL,  // assign(index, value) function pointer
    NULL  // resize(index) function pointer
  }
};

static const rosidl_typesupport_introspection_c__MessageMembers robot_param_atomic__srv__UpdateConfig_Response__rosidl_typesupport_introspection_c__UpdateConfig_Response_message_members = {
  "robot_param_atomic__srv",  // message namespace
  "UpdateConfig_Response",  // message name
  7,  // number of fields
  sizeof(robot_param_atomic__srv__UpdateConfig_Response),
  false,  // has_any_key_member_
  robot_param_atomic__srv__UpdateConfig_Response__rosidl_typesupport_introspection_c__UpdateConfig_Response_message_member_array,  // message members
  robot_param_atomic__srv__UpdateConfig_Response__rosidl_typesupport_introspection_c__UpdateConfig_Response_init_function,  // function to initialize message memory (memory has to be allocated)
  robot_param_atomic__srv__UpdateConfig_Response__rosidl_typesupport_introspection_c__UpdateConfig_Response_fini_function  // function to terminate message instance (will not free memory)
};

// this is not const since it must be initialized on first access
// since C does not allow non-integral compile-time constants
static rosidl_message_type_support_t robot_param_atomic__srv__UpdateConfig_Response__rosidl_typesupport_introspection_c__UpdateConfig_Response_message_type_support_handle = {
  0,
  &robot_param_atomic__srv__UpdateConfig_Response__rosidl_typesupport_introspection_c__UpdateConfig_Response_message_members,
  get_message_typesupport_handle_function,
  &robot_param_atomic__srv__UpdateConfig_Response__get_type_hash,
  &robot_param_atomic__srv__UpdateConfig_Response__get_type_description,
  &robot_param_atomic__srv__UpdateConfig_Response__get_type_description_sources,
};

ROSIDL_TYPESUPPORT_INTROSPECTION_C_EXPORT_robot_param_atomic
const rosidl_message_type_support_t *
ROSIDL_TYPESUPPORT_INTERFACE__MESSAGE_SYMBOL_NAME(rosidl_typesupport_introspection_c, robot_param_atomic, srv, UpdateConfig_Response)() {
  if (!robot_param_atomic__srv__UpdateConfig_Response__rosidl_typesupport_introspection_c__UpdateConfig_Response_message_type_support_handle.typesupport_identifier) {
    robot_param_atomic__srv__UpdateConfig_Response__rosidl_typesupport_introspection_c__UpdateConfig_Response_message_type_support_handle.typesupport_identifier =
      rosidl_typesupport_introspection_c__identifier;
  }
  return &robot_param_atomic__srv__UpdateConfig_Response__rosidl_typesupport_introspection_c__UpdateConfig_Response_message_type_support_handle;
}
#ifdef __cplusplus
}
#endif

// already included above
// #include <stddef.h>
// already included above
// #include "robot_param_atomic/srv/detail/update_config__rosidl_typesupport_introspection_c.h"
// already included above
// #include "robot_param_atomic/msg/rosidl_typesupport_introspection_c__visibility_control.h"
// already included above
// #include "rosidl_typesupport_introspection_c/field_types.h"
// already included above
// #include "rosidl_typesupport_introspection_c/identifier.h"
// already included above
// #include "rosidl_typesupport_introspection_c/message_introspection.h"
// already included above
// #include "robot_param_atomic/srv/detail/update_config__functions.h"
// already included above
// #include "robot_param_atomic/srv/detail/update_config__struct.h"


// Include directives for member types
// Member `info`
#include "service_msgs/msg/service_event_info.h"
// Member `info`
#include "service_msgs/msg/detail/service_event_info__rosidl_typesupport_introspection_c.h"
// Member `request`
// Member `response`
#include "robot_param_atomic/srv/update_config.h"
// Member `request`
// Member `response`
// already included above
// #include "robot_param_atomic/srv/detail/update_config__rosidl_typesupport_introspection_c.h"

#ifdef __cplusplus
extern "C"
{
#endif

void robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__UpdateConfig_Event_init_function(
  void * message_memory, enum rosidl_runtime_c__message_initialization _init)
{
  // TODO(karsten1987): initializers are not yet implemented for typesupport c
  // see https://github.com/ros2/ros2/issues/397
  (void) _init;
  robot_param_atomic__srv__UpdateConfig_Event__init(message_memory);
}

void robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__UpdateConfig_Event_fini_function(void * message_memory)
{
  robot_param_atomic__srv__UpdateConfig_Event__fini(message_memory);
}

size_t robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__size_function__UpdateConfig_Event__request(
  const void * untyped_member)
{
  const robot_param_atomic__srv__UpdateConfig_Request__Sequence * member =
    (const robot_param_atomic__srv__UpdateConfig_Request__Sequence *)(untyped_member);
  return member->size;
}

const void * robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__get_const_function__UpdateConfig_Event__request(
  const void * untyped_member, size_t index)
{
  const robot_param_atomic__srv__UpdateConfig_Request__Sequence * member =
    (const robot_param_atomic__srv__UpdateConfig_Request__Sequence *)(untyped_member);
  return &member->data[index];
}

void * robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__get_function__UpdateConfig_Event__request(
  void * untyped_member, size_t index)
{
  robot_param_atomic__srv__UpdateConfig_Request__Sequence * member =
    (robot_param_atomic__srv__UpdateConfig_Request__Sequence *)(untyped_member);
  return &member->data[index];
}

void robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__fetch_function__UpdateConfig_Event__request(
  const void * untyped_member, size_t index, void * untyped_value)
{
  const robot_param_atomic__srv__UpdateConfig_Request * item =
    ((const robot_param_atomic__srv__UpdateConfig_Request *)
    robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__get_const_function__UpdateConfig_Event__request(untyped_member, index));
  robot_param_atomic__srv__UpdateConfig_Request * value =
    (robot_param_atomic__srv__UpdateConfig_Request *)(untyped_value);
  *value = *item;
}

void robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__assign_function__UpdateConfig_Event__request(
  void * untyped_member, size_t index, const void * untyped_value)
{
  robot_param_atomic__srv__UpdateConfig_Request * item =
    ((robot_param_atomic__srv__UpdateConfig_Request *)
    robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__get_function__UpdateConfig_Event__request(untyped_member, index));
  const robot_param_atomic__srv__UpdateConfig_Request * value =
    (const robot_param_atomic__srv__UpdateConfig_Request *)(untyped_value);
  *item = *value;
}

bool robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__resize_function__UpdateConfig_Event__request(
  void * untyped_member, size_t size)
{
  robot_param_atomic__srv__UpdateConfig_Request__Sequence * member =
    (robot_param_atomic__srv__UpdateConfig_Request__Sequence *)(untyped_member);
  robot_param_atomic__srv__UpdateConfig_Request__Sequence__fini(member);
  return robot_param_atomic__srv__UpdateConfig_Request__Sequence__init(member, size);
}

size_t robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__size_function__UpdateConfig_Event__response(
  const void * untyped_member)
{
  const robot_param_atomic__srv__UpdateConfig_Response__Sequence * member =
    (const robot_param_atomic__srv__UpdateConfig_Response__Sequence *)(untyped_member);
  return member->size;
}

const void * robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__get_const_function__UpdateConfig_Event__response(
  const void * untyped_member, size_t index)
{
  const robot_param_atomic__srv__UpdateConfig_Response__Sequence * member =
    (const robot_param_atomic__srv__UpdateConfig_Response__Sequence *)(untyped_member);
  return &member->data[index];
}

void * robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__get_function__UpdateConfig_Event__response(
  void * untyped_member, size_t index)
{
  robot_param_atomic__srv__UpdateConfig_Response__Sequence * member =
    (robot_param_atomic__srv__UpdateConfig_Response__Sequence *)(untyped_member);
  return &member->data[index];
}

void robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__fetch_function__UpdateConfig_Event__response(
  const void * untyped_member, size_t index, void * untyped_value)
{
  const robot_param_atomic__srv__UpdateConfig_Response * item =
    ((const robot_param_atomic__srv__UpdateConfig_Response *)
    robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__get_const_function__UpdateConfig_Event__response(untyped_member, index));
  robot_param_atomic__srv__UpdateConfig_Response * value =
    (robot_param_atomic__srv__UpdateConfig_Response *)(untyped_value);
  *value = *item;
}

void robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__assign_function__UpdateConfig_Event__response(
  void * untyped_member, size_t index, const void * untyped_value)
{
  robot_param_atomic__srv__UpdateConfig_Response * item =
    ((robot_param_atomic__srv__UpdateConfig_Response *)
    robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__get_function__UpdateConfig_Event__response(untyped_member, index));
  const robot_param_atomic__srv__UpdateConfig_Response * value =
    (const robot_param_atomic__srv__UpdateConfig_Response *)(untyped_value);
  *item = *value;
}

bool robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__resize_function__UpdateConfig_Event__response(
  void * untyped_member, size_t size)
{
  robot_param_atomic__srv__UpdateConfig_Response__Sequence * member =
    (robot_param_atomic__srv__UpdateConfig_Response__Sequence *)(untyped_member);
  robot_param_atomic__srv__UpdateConfig_Response__Sequence__fini(member);
  return robot_param_atomic__srv__UpdateConfig_Response__Sequence__init(member, size);
}

static rosidl_typesupport_introspection_c__MessageMember robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__UpdateConfig_Event_message_member_array[3] = {
  {
    "info",  // name
    rosidl_typesupport_introspection_c__ROS_TYPE_MESSAGE,  // type
    0,  // upper bound of string
    NULL,  // members of sub message (initialized later)
    false,  // is key
    false,  // is array
    0,  // array size
    false,  // is upper bound
    offsetof(robot_param_atomic__srv__UpdateConfig_Event, info),  // bytes offset in struct
    NULL,  // default value
    NULL,  // size() function pointer
    NULL,  // get_const(index) function pointer
    NULL,  // get(index) function pointer
    NULL,  // fetch(index, &value) function pointer
    NULL,  // assign(index, value) function pointer
    NULL  // resize(index) function pointer
  },
  {
    "request",  // name
    rosidl_typesupport_introspection_c__ROS_TYPE_MESSAGE,  // type
    0,  // upper bound of string
    NULL,  // members of sub message (initialized later)
    false,  // is key
    true,  // is array
    1,  // array size
    true,  // is upper bound
    offsetof(robot_param_atomic__srv__UpdateConfig_Event, request),  // bytes offset in struct
    NULL,  // default value
    robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__size_function__UpdateConfig_Event__request,  // size() function pointer
    robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__get_const_function__UpdateConfig_Event__request,  // get_const(index) function pointer
    robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__get_function__UpdateConfig_Event__request,  // get(index) function pointer
    robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__fetch_function__UpdateConfig_Event__request,  // fetch(index, &value) function pointer
    robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__assign_function__UpdateConfig_Event__request,  // assign(index, value) function pointer
    robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__resize_function__UpdateConfig_Event__request  // resize(index) function pointer
  },
  {
    "response",  // name
    rosidl_typesupport_introspection_c__ROS_TYPE_MESSAGE,  // type
    0,  // upper bound of string
    NULL,  // members of sub message (initialized later)
    false,  // is key
    true,  // is array
    1,  // array size
    true,  // is upper bound
    offsetof(robot_param_atomic__srv__UpdateConfig_Event, response),  // bytes offset in struct
    NULL,  // default value
    robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__size_function__UpdateConfig_Event__response,  // size() function pointer
    robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__get_const_function__UpdateConfig_Event__response,  // get_const(index) function pointer
    robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__get_function__UpdateConfig_Event__response,  // get(index) function pointer
    robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__fetch_function__UpdateConfig_Event__response,  // fetch(index, &value) function pointer
    robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__assign_function__UpdateConfig_Event__response,  // assign(index, value) function pointer
    robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__resize_function__UpdateConfig_Event__response  // resize(index) function pointer
  }
};

static const rosidl_typesupport_introspection_c__MessageMembers robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__UpdateConfig_Event_message_members = {
  "robot_param_atomic__srv",  // message namespace
  "UpdateConfig_Event",  // message name
  3,  // number of fields
  sizeof(robot_param_atomic__srv__UpdateConfig_Event),
  false,  // has_any_key_member_
  robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__UpdateConfig_Event_message_member_array,  // message members
  robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__UpdateConfig_Event_init_function,  // function to initialize message memory (memory has to be allocated)
  robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__UpdateConfig_Event_fini_function  // function to terminate message instance (will not free memory)
};

// this is not const since it must be initialized on first access
// since C does not allow non-integral compile-time constants
static rosidl_message_type_support_t robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__UpdateConfig_Event_message_type_support_handle = {
  0,
  &robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__UpdateConfig_Event_message_members,
  get_message_typesupport_handle_function,
  &robot_param_atomic__srv__UpdateConfig_Event__get_type_hash,
  &robot_param_atomic__srv__UpdateConfig_Event__get_type_description,
  &robot_param_atomic__srv__UpdateConfig_Event__get_type_description_sources,
};

ROSIDL_TYPESUPPORT_INTROSPECTION_C_EXPORT_robot_param_atomic
const rosidl_message_type_support_t *
ROSIDL_TYPESUPPORT_INTERFACE__MESSAGE_SYMBOL_NAME(rosidl_typesupport_introspection_c, robot_param_atomic, srv, UpdateConfig_Event)() {
  robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__UpdateConfig_Event_message_member_array[0].members_ =
    ROSIDL_TYPESUPPORT_INTERFACE__MESSAGE_SYMBOL_NAME(rosidl_typesupport_introspection_c, service_msgs, msg, ServiceEventInfo)();
  robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__UpdateConfig_Event_message_member_array[1].members_ =
    ROSIDL_TYPESUPPORT_INTERFACE__MESSAGE_SYMBOL_NAME(rosidl_typesupport_introspection_c, robot_param_atomic, srv, UpdateConfig_Request)();
  robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__UpdateConfig_Event_message_member_array[2].members_ =
    ROSIDL_TYPESUPPORT_INTERFACE__MESSAGE_SYMBOL_NAME(rosidl_typesupport_introspection_c, robot_param_atomic, srv, UpdateConfig_Response)();
  if (!robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__UpdateConfig_Event_message_type_support_handle.typesupport_identifier) {
    robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__UpdateConfig_Event_message_type_support_handle.typesupport_identifier =
      rosidl_typesupport_introspection_c__identifier;
  }
  return &robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__UpdateConfig_Event_message_type_support_handle;
}
#ifdef __cplusplus
}
#endif

#include "rosidl_runtime_c/service_type_support_struct.h"
// already included above
// #include "robot_param_atomic/msg/rosidl_typesupport_introspection_c__visibility_control.h"
// already included above
// #include "robot_param_atomic/srv/detail/update_config__rosidl_typesupport_introspection_c.h"
// already included above
// #include "rosidl_typesupport_introspection_c/identifier.h"
#include "rosidl_typesupport_introspection_c/service_introspection.h"

// this is intentionally not const to allow initialization later to prevent an initialization race
static rosidl_typesupport_introspection_c__ServiceMembers robot_param_atomic__srv__detail__update_config__rosidl_typesupport_introspection_c__UpdateConfig_service_members = {
  "robot_param_atomic__srv",  // service namespace
  "UpdateConfig",  // service name
  // the following fields are initialized below on first access
  NULL,  // request message
  // robot_param_atomic__srv__detail__update_config__rosidl_typesupport_introspection_c__UpdateConfig_Request_message_type_support_handle,
  NULL,  // response message
  // robot_param_atomic__srv__detail__update_config__rosidl_typesupport_introspection_c__UpdateConfig_Response_message_type_support_handle
  NULL  // event_message
  // robot_param_atomic__srv__detail__update_config__rosidl_typesupport_introspection_c__UpdateConfig_Response_message_type_support_handle
};


static rosidl_service_type_support_t robot_param_atomic__srv__detail__update_config__rosidl_typesupport_introspection_c__UpdateConfig_service_type_support_handle = {
  0,
  &robot_param_atomic__srv__detail__update_config__rosidl_typesupport_introspection_c__UpdateConfig_service_members,
  get_service_typesupport_handle_function,
  &robot_param_atomic__srv__UpdateConfig_Request__rosidl_typesupport_introspection_c__UpdateConfig_Request_message_type_support_handle,
  &robot_param_atomic__srv__UpdateConfig_Response__rosidl_typesupport_introspection_c__UpdateConfig_Response_message_type_support_handle,
  &robot_param_atomic__srv__UpdateConfig_Event__rosidl_typesupport_introspection_c__UpdateConfig_Event_message_type_support_handle,
  ROSIDL_TYPESUPPORT_INTERFACE__SERVICE_CREATE_EVENT_MESSAGE_SYMBOL_NAME(
    rosidl_typesupport_c,
    robot_param_atomic,
    srv,
    UpdateConfig
  ),
  ROSIDL_TYPESUPPORT_INTERFACE__SERVICE_DESTROY_EVENT_MESSAGE_SYMBOL_NAME(
    rosidl_typesupport_c,
    robot_param_atomic,
    srv,
    UpdateConfig
  ),
  &robot_param_atomic__srv__UpdateConfig__get_type_hash,
  &robot_param_atomic__srv__UpdateConfig__get_type_description,
  &robot_param_atomic__srv__UpdateConfig__get_type_description_sources,
};

// Forward declaration of message type support functions for service members
const rosidl_message_type_support_t *
ROSIDL_TYPESUPPORT_INTERFACE__MESSAGE_SYMBOL_NAME(rosidl_typesupport_introspection_c, robot_param_atomic, srv, UpdateConfig_Request)(void);

const rosidl_message_type_support_t *
ROSIDL_TYPESUPPORT_INTERFACE__MESSAGE_SYMBOL_NAME(rosidl_typesupport_introspection_c, robot_param_atomic, srv, UpdateConfig_Response)(void);

const rosidl_message_type_support_t *
ROSIDL_TYPESUPPORT_INTERFACE__MESSAGE_SYMBOL_NAME(rosidl_typesupport_introspection_c, robot_param_atomic, srv, UpdateConfig_Event)(void);

ROSIDL_TYPESUPPORT_INTROSPECTION_C_EXPORT_robot_param_atomic
const rosidl_service_type_support_t *
ROSIDL_TYPESUPPORT_INTERFACE__SERVICE_SYMBOL_NAME(rosidl_typesupport_introspection_c, robot_param_atomic, srv, UpdateConfig)(void) {
  if (!robot_param_atomic__srv__detail__update_config__rosidl_typesupport_introspection_c__UpdateConfig_service_type_support_handle.typesupport_identifier) {
    robot_param_atomic__srv__detail__update_config__rosidl_typesupport_introspection_c__UpdateConfig_service_type_support_handle.typesupport_identifier =
      rosidl_typesupport_introspection_c__identifier;
  }
  rosidl_typesupport_introspection_c__ServiceMembers * service_members =
    (rosidl_typesupport_introspection_c__ServiceMembers *)robot_param_atomic__srv__detail__update_config__rosidl_typesupport_introspection_c__UpdateConfig_service_type_support_handle.data;

  if (!service_members->request_members_) {
    service_members->request_members_ =
      (const rosidl_typesupport_introspection_c__MessageMembers *)
      ROSIDL_TYPESUPPORT_INTERFACE__MESSAGE_SYMBOL_NAME(rosidl_typesupport_introspection_c, robot_param_atomic, srv, UpdateConfig_Request)()->data;
  }
  if (!service_members->response_members_) {
    service_members->response_members_ =
      (const rosidl_typesupport_introspection_c__MessageMembers *)
      ROSIDL_TYPESUPPORT_INTERFACE__MESSAGE_SYMBOL_NAME(rosidl_typesupport_introspection_c, robot_param_atomic, srv, UpdateConfig_Response)()->data;
  }
  if (!service_members->event_members_) {
    service_members->event_members_ =
      (const rosidl_typesupport_introspection_c__MessageMembers *)
      ROSIDL_TYPESUPPORT_INTERFACE__MESSAGE_SYMBOL_NAME(rosidl_typesupport_introspection_c, robot_param_atomic, srv, UpdateConfig_Event)()->data;
  }

  return &robot_param_atomic__srv__detail__update_config__rosidl_typesupport_introspection_c__UpdateConfig_service_type_support_handle;
}
