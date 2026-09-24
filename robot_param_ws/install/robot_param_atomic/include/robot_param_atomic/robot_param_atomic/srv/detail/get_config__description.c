// generated from rosidl_generator_c/resource/idl__description.c.em
// with input from robot_param_atomic:srv/GetConfig.idl
// generated code does not contain a copyright notice

#include "robot_param_atomic/srv/detail/get_config__functions.h"

ROSIDL_GENERATOR_C_PUBLIC_robot_param_atomic
const rosidl_type_hash_t *
robot_param_atomic__srv__GetConfig__get_type_hash(
  const rosidl_service_type_support_t * type_support)
{
  (void)type_support;
  static rosidl_type_hash_t hash = {1, {
      0xc6, 0x4b, 0xc8, 0x05, 0xde, 0xed, 0xf7, 0xa4,
      0xf1, 0x1a, 0x94, 0x6a, 0xab, 0xae, 0xaa, 0x51,
      0x1b, 0x27, 0x84, 0x3b, 0x5d, 0x8f, 0xad, 0x0b,
      0x73, 0x4a, 0xda, 0xb7, 0x0d, 0x75, 0xd1, 0x18,
    }};
  return &hash;
}

ROSIDL_GENERATOR_C_PUBLIC_robot_param_atomic
const rosidl_type_hash_t *
robot_param_atomic__srv__GetConfig_Request__get_type_hash(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static rosidl_type_hash_t hash = {1, {
      0xb4, 0x92, 0x4d, 0xb8, 0xdd, 0x9f, 0x25, 0x87,
      0x10, 0x29, 0x6d, 0x84, 0x21, 0x13, 0xf5, 0xd1,
      0x93, 0xfa, 0x6f, 0x30, 0x71, 0xac, 0x10, 0x70,
      0x32, 0x07, 0x95, 0x68, 0x75, 0xfc, 0x01, 0x3f,
    }};
  return &hash;
}

ROSIDL_GENERATOR_C_PUBLIC_robot_param_atomic
const rosidl_type_hash_t *
robot_param_atomic__srv__GetConfig_Response__get_type_hash(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static rosidl_type_hash_t hash = {1, {
      0xef, 0x77, 0xc2, 0xee, 0x7b, 0x75, 0x9b, 0xdd,
      0x99, 0x59, 0xf1, 0xe7, 0x62, 0x7f, 0x0f, 0x3d,
      0x3b, 0x00, 0xfe, 0x01, 0xed, 0x12, 0x26, 0xc2,
      0xcf, 0xe1, 0xe8, 0xd9, 0x46, 0x67, 0x9e, 0xa1,
    }};
  return &hash;
}

ROSIDL_GENERATOR_C_PUBLIC_robot_param_atomic
const rosidl_type_hash_t *
robot_param_atomic__srv__GetConfig_Event__get_type_hash(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static rosidl_type_hash_t hash = {1, {
      0xcf, 0xc9, 0xad, 0x00, 0xb1, 0x1f, 0x67, 0x0d,
      0xb8, 0x89, 0xcd, 0xf8, 0x3d, 0xd5, 0xb4, 0x98,
      0x13, 0x2b, 0x5c, 0xcd, 0xcc, 0x32, 0x65, 0x81,
      0xcd, 0xf0, 0xde, 0x5b, 0x22, 0x0e, 0xd6, 0xb1,
    }};
  return &hash;
}

#include <assert.h>
#include <string.h>

// Include directives for referenced types
#include "builtin_interfaces/msg/detail/time__functions.h"
#include "service_msgs/msg/detail/service_event_info__functions.h"

// Hashes for external referenced types
#ifndef NDEBUG
static const rosidl_type_hash_t builtin_interfaces__msg__Time__EXPECTED_HASH = {1, {
    0xb1, 0x06, 0x23, 0x5e, 0x25, 0xa4, 0xc5, 0xed,
    0x35, 0x09, 0x8a, 0xa0, 0xa6, 0x1a, 0x3e, 0xe9,
    0xc9, 0xb1, 0x8d, 0x19, 0x7f, 0x39, 0x8b, 0x0e,
    0x42, 0x06, 0xce, 0xa9, 0xac, 0xf9, 0xc1, 0x97,
  }};
static const rosidl_type_hash_t service_msgs__msg__ServiceEventInfo__EXPECTED_HASH = {1, {
    0x41, 0xbc, 0xbb, 0xe0, 0x7a, 0x75, 0xc9, 0xb5,
    0x2b, 0xc9, 0x6b, 0xfd, 0x5c, 0x24, 0xd7, 0xf0,
    0xfc, 0x0a, 0x08, 0xc0, 0xcb, 0x79, 0x21, 0xb3,
    0x37, 0x3c, 0x57, 0x32, 0x34, 0x5a, 0x6f, 0x45,
  }};
#endif

static char robot_param_atomic__srv__GetConfig__TYPE_NAME[] = "robot_param_atomic/srv/GetConfig";
static char builtin_interfaces__msg__Time__TYPE_NAME[] = "builtin_interfaces/msg/Time";
static char robot_param_atomic__srv__GetConfig_Event__TYPE_NAME[] = "robot_param_atomic/srv/GetConfig_Event";
static char robot_param_atomic__srv__GetConfig_Request__TYPE_NAME[] = "robot_param_atomic/srv/GetConfig_Request";
static char robot_param_atomic__srv__GetConfig_Response__TYPE_NAME[] = "robot_param_atomic/srv/GetConfig_Response";
static char service_msgs__msg__ServiceEventInfo__TYPE_NAME[] = "service_msgs/msg/ServiceEventInfo";

// Define type names, field names, and default values
static char robot_param_atomic__srv__GetConfig__FIELD_NAME__request_message[] = "request_message";
static char robot_param_atomic__srv__GetConfig__FIELD_NAME__response_message[] = "response_message";
static char robot_param_atomic__srv__GetConfig__FIELD_NAME__event_message[] = "event_message";

static rosidl_runtime_c__type_description__Field robot_param_atomic__srv__GetConfig__FIELDS[] = {
  {
    {robot_param_atomic__srv__GetConfig__FIELD_NAME__request_message, 15, 15},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_NESTED_TYPE,
      0,
      0,
      {robot_param_atomic__srv__GetConfig_Request__TYPE_NAME, 40, 40},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__GetConfig__FIELD_NAME__response_message, 16, 16},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_NESTED_TYPE,
      0,
      0,
      {robot_param_atomic__srv__GetConfig_Response__TYPE_NAME, 41, 41},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__GetConfig__FIELD_NAME__event_message, 13, 13},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_NESTED_TYPE,
      0,
      0,
      {robot_param_atomic__srv__GetConfig_Event__TYPE_NAME, 38, 38},
    },
    {NULL, 0, 0},
  },
};

static rosidl_runtime_c__type_description__IndividualTypeDescription robot_param_atomic__srv__GetConfig__REFERENCED_TYPE_DESCRIPTIONS[] = {
  {
    {builtin_interfaces__msg__Time__TYPE_NAME, 27, 27},
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__GetConfig_Event__TYPE_NAME, 38, 38},
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__GetConfig_Request__TYPE_NAME, 40, 40},
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__GetConfig_Response__TYPE_NAME, 41, 41},
    {NULL, 0, 0},
  },
  {
    {service_msgs__msg__ServiceEventInfo__TYPE_NAME, 33, 33},
    {NULL, 0, 0},
  },
};

const rosidl_runtime_c__type_description__TypeDescription *
robot_param_atomic__srv__GetConfig__get_type_description(
  const rosidl_service_type_support_t * type_support)
{
  (void)type_support;
  static bool constructed = false;
  static const rosidl_runtime_c__type_description__TypeDescription description = {
    {
      {robot_param_atomic__srv__GetConfig__TYPE_NAME, 32, 32},
      {robot_param_atomic__srv__GetConfig__FIELDS, 3, 3},
    },
    {robot_param_atomic__srv__GetConfig__REFERENCED_TYPE_DESCRIPTIONS, 5, 5},
  };
  if (!constructed) {
    assert(0 == memcmp(&builtin_interfaces__msg__Time__EXPECTED_HASH, builtin_interfaces__msg__Time__get_type_hash(NULL), sizeof(rosidl_type_hash_t)));
    description.referenced_type_descriptions.data[0].fields = builtin_interfaces__msg__Time__get_type_description(NULL)->type_description.fields;
    description.referenced_type_descriptions.data[1].fields = robot_param_atomic__srv__GetConfig_Event__get_type_description(NULL)->type_description.fields;
    description.referenced_type_descriptions.data[2].fields = robot_param_atomic__srv__GetConfig_Request__get_type_description(NULL)->type_description.fields;
    description.referenced_type_descriptions.data[3].fields = robot_param_atomic__srv__GetConfig_Response__get_type_description(NULL)->type_description.fields;
    assert(0 == memcmp(&service_msgs__msg__ServiceEventInfo__EXPECTED_HASH, service_msgs__msg__ServiceEventInfo__get_type_hash(NULL), sizeof(rosidl_type_hash_t)));
    description.referenced_type_descriptions.data[4].fields = service_msgs__msg__ServiceEventInfo__get_type_description(NULL)->type_description.fields;
    constructed = true;
  }
  return &description;
}
// Define type names, field names, and default values
static char robot_param_atomic__srv__GetConfig_Request__FIELD_NAME__structure_needs_at_least_one_member[] = "structure_needs_at_least_one_member";

static rosidl_runtime_c__type_description__Field robot_param_atomic__srv__GetConfig_Request__FIELDS[] = {
  {
    {robot_param_atomic__srv__GetConfig_Request__FIELD_NAME__structure_needs_at_least_one_member, 35, 35},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_UINT8,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
};

const rosidl_runtime_c__type_description__TypeDescription *
robot_param_atomic__srv__GetConfig_Request__get_type_description(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static bool constructed = false;
  static const rosidl_runtime_c__type_description__TypeDescription description = {
    {
      {robot_param_atomic__srv__GetConfig_Request__TYPE_NAME, 40, 40},
      {robot_param_atomic__srv__GetConfig_Request__FIELDS, 1, 1},
    },
    {NULL, 0, 0},
  };
  if (!constructed) {
    constructed = true;
  }
  return &description;
}
// Define type names, field names, and default values
static char robot_param_atomic__srv__GetConfig_Response__FIELD_NAME__version[] = "version";
static char robot_param_atomic__srv__GetConfig_Response__FIELD_NAME__sampling_rate_hz[] = "sampling_rate_hz";
static char robot_param_atomic__srv__GetConfig_Response__FIELD_NAME__cache_length_s[] = "cache_length_s";
static char robot_param_atomic__srv__GetConfig_Response__FIELD_NAME__allowed_latency_s[] = "allowed_latency_s";
static char robot_param_atomic__srv__GetConfig_Response__FIELD_NAME__config_hash[] = "config_hash";
static char robot_param_atomic__srv__GetConfig_Response__FIELD_NAME__commit_seq[] = "commit_seq";

static rosidl_runtime_c__type_description__Field robot_param_atomic__srv__GetConfig_Response__FIELDS[] = {
  {
    {robot_param_atomic__srv__GetConfig_Response__FIELD_NAME__version, 7, 7},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_DOUBLE,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__GetConfig_Response__FIELD_NAME__sampling_rate_hz, 16, 16},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_DOUBLE,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__GetConfig_Response__FIELD_NAME__cache_length_s, 14, 14},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_DOUBLE,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__GetConfig_Response__FIELD_NAME__allowed_latency_s, 17, 17},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_DOUBLE,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__GetConfig_Response__FIELD_NAME__config_hash, 11, 11},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_STRING,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__GetConfig_Response__FIELD_NAME__commit_seq, 10, 10},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_INT64,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
};

const rosidl_runtime_c__type_description__TypeDescription *
robot_param_atomic__srv__GetConfig_Response__get_type_description(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static bool constructed = false;
  static const rosidl_runtime_c__type_description__TypeDescription description = {
    {
      {robot_param_atomic__srv__GetConfig_Response__TYPE_NAME, 41, 41},
      {robot_param_atomic__srv__GetConfig_Response__FIELDS, 6, 6},
    },
    {NULL, 0, 0},
  };
  if (!constructed) {
    constructed = true;
  }
  return &description;
}
// Define type names, field names, and default values
static char robot_param_atomic__srv__GetConfig_Event__FIELD_NAME__info[] = "info";
static char robot_param_atomic__srv__GetConfig_Event__FIELD_NAME__request[] = "request";
static char robot_param_atomic__srv__GetConfig_Event__FIELD_NAME__response[] = "response";

static rosidl_runtime_c__type_description__Field robot_param_atomic__srv__GetConfig_Event__FIELDS[] = {
  {
    {robot_param_atomic__srv__GetConfig_Event__FIELD_NAME__info, 4, 4},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_NESTED_TYPE,
      0,
      0,
      {service_msgs__msg__ServiceEventInfo__TYPE_NAME, 33, 33},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__GetConfig_Event__FIELD_NAME__request, 7, 7},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_NESTED_TYPE_BOUNDED_SEQUENCE,
      1,
      0,
      {robot_param_atomic__srv__GetConfig_Request__TYPE_NAME, 40, 40},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__GetConfig_Event__FIELD_NAME__response, 8, 8},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_NESTED_TYPE_BOUNDED_SEQUENCE,
      1,
      0,
      {robot_param_atomic__srv__GetConfig_Response__TYPE_NAME, 41, 41},
    },
    {NULL, 0, 0},
  },
};

static rosidl_runtime_c__type_description__IndividualTypeDescription robot_param_atomic__srv__GetConfig_Event__REFERENCED_TYPE_DESCRIPTIONS[] = {
  {
    {builtin_interfaces__msg__Time__TYPE_NAME, 27, 27},
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__GetConfig_Request__TYPE_NAME, 40, 40},
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__GetConfig_Response__TYPE_NAME, 41, 41},
    {NULL, 0, 0},
  },
  {
    {service_msgs__msg__ServiceEventInfo__TYPE_NAME, 33, 33},
    {NULL, 0, 0},
  },
};

const rosidl_runtime_c__type_description__TypeDescription *
robot_param_atomic__srv__GetConfig_Event__get_type_description(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static bool constructed = false;
  static const rosidl_runtime_c__type_description__TypeDescription description = {
    {
      {robot_param_atomic__srv__GetConfig_Event__TYPE_NAME, 38, 38},
      {robot_param_atomic__srv__GetConfig_Event__FIELDS, 3, 3},
    },
    {robot_param_atomic__srv__GetConfig_Event__REFERENCED_TYPE_DESCRIPTIONS, 4, 4},
  };
  if (!constructed) {
    assert(0 == memcmp(&builtin_interfaces__msg__Time__EXPECTED_HASH, builtin_interfaces__msg__Time__get_type_hash(NULL), sizeof(rosidl_type_hash_t)));
    description.referenced_type_descriptions.data[0].fields = builtin_interfaces__msg__Time__get_type_description(NULL)->type_description.fields;
    description.referenced_type_descriptions.data[1].fields = robot_param_atomic__srv__GetConfig_Request__get_type_description(NULL)->type_description.fields;
    description.referenced_type_descriptions.data[2].fields = robot_param_atomic__srv__GetConfig_Response__get_type_description(NULL)->type_description.fields;
    assert(0 == memcmp(&service_msgs__msg__ServiceEventInfo__EXPECTED_HASH, service_msgs__msg__ServiceEventInfo__get_type_hash(NULL), sizeof(rosidl_type_hash_t)));
    description.referenced_type_descriptions.data[3].fields = service_msgs__msg__ServiceEventInfo__get_type_description(NULL)->type_description.fields;
    constructed = true;
  }
  return &description;
}

static char toplevel_type_raw_source[] =
  "# Read the currently committed configuration snapshot.\n"
  "# Always returns the whole snapshot so a client never observes a torn\n"
  "# half-updated state.\n"
  "\n"
  "---\n"
  "float64 version\n"
  "float64 sampling_rate_hz\n"
  "float64 cache_length_s\n"
  "float64 allowed_latency_s\n"
  "# SHA-256 hex digest of the canonical JSON snapshot that was persisted.\n"
  "string config_hash\n"
  "# Monotonic sequence incremented for every successfully committed update.\n"
  "int64 commit_seq";

static char srv_encoding[] = "srv";
static char implicit_encoding[] = "implicit";

// Define all individual source functions

const rosidl_runtime_c__type_description__TypeSource *
robot_param_atomic__srv__GetConfig__get_individual_type_description_source(
  const rosidl_service_type_support_t * type_support)
{
  (void)type_support;
  static const rosidl_runtime_c__type_description__TypeSource source = {
    {robot_param_atomic__srv__GetConfig__TYPE_NAME, 32, 32},
    {srv_encoding, 3, 3},
    {toplevel_type_raw_source, 424, 424},
  };
  return &source;
}

const rosidl_runtime_c__type_description__TypeSource *
robot_param_atomic__srv__GetConfig_Request__get_individual_type_description_source(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static const rosidl_runtime_c__type_description__TypeSource source = {
    {robot_param_atomic__srv__GetConfig_Request__TYPE_NAME, 40, 40},
    {implicit_encoding, 8, 8},
    {NULL, 0, 0},
  };
  return &source;
}

const rosidl_runtime_c__type_description__TypeSource *
robot_param_atomic__srv__GetConfig_Response__get_individual_type_description_source(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static const rosidl_runtime_c__type_description__TypeSource source = {
    {robot_param_atomic__srv__GetConfig_Response__TYPE_NAME, 41, 41},
    {implicit_encoding, 8, 8},
    {NULL, 0, 0},
  };
  return &source;
}

const rosidl_runtime_c__type_description__TypeSource *
robot_param_atomic__srv__GetConfig_Event__get_individual_type_description_source(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static const rosidl_runtime_c__type_description__TypeSource source = {
    {robot_param_atomic__srv__GetConfig_Event__TYPE_NAME, 38, 38},
    {implicit_encoding, 8, 8},
    {NULL, 0, 0},
  };
  return &source;
}

const rosidl_runtime_c__type_description__TypeSource__Sequence *
robot_param_atomic__srv__GetConfig__get_type_description_sources(
  const rosidl_service_type_support_t * type_support)
{
  (void)type_support;
  static rosidl_runtime_c__type_description__TypeSource sources[6];
  static const rosidl_runtime_c__type_description__TypeSource__Sequence source_sequence = {sources, 6, 6};
  static bool constructed = false;
  if (!constructed) {
    sources[0] = *robot_param_atomic__srv__GetConfig__get_individual_type_description_source(NULL),
    sources[1] = *builtin_interfaces__msg__Time__get_individual_type_description_source(NULL);
    sources[2] = *robot_param_atomic__srv__GetConfig_Event__get_individual_type_description_source(NULL);
    sources[3] = *robot_param_atomic__srv__GetConfig_Request__get_individual_type_description_source(NULL);
    sources[4] = *robot_param_atomic__srv__GetConfig_Response__get_individual_type_description_source(NULL);
    sources[5] = *service_msgs__msg__ServiceEventInfo__get_individual_type_description_source(NULL);
    constructed = true;
  }
  return &source_sequence;
}

const rosidl_runtime_c__type_description__TypeSource__Sequence *
robot_param_atomic__srv__GetConfig_Request__get_type_description_sources(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static rosidl_runtime_c__type_description__TypeSource sources[1];
  static const rosidl_runtime_c__type_description__TypeSource__Sequence source_sequence = {sources, 1, 1};
  static bool constructed = false;
  if (!constructed) {
    sources[0] = *robot_param_atomic__srv__GetConfig_Request__get_individual_type_description_source(NULL),
    constructed = true;
  }
  return &source_sequence;
}

const rosidl_runtime_c__type_description__TypeSource__Sequence *
robot_param_atomic__srv__GetConfig_Response__get_type_description_sources(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static rosidl_runtime_c__type_description__TypeSource sources[1];
  static const rosidl_runtime_c__type_description__TypeSource__Sequence source_sequence = {sources, 1, 1};
  static bool constructed = false;
  if (!constructed) {
    sources[0] = *robot_param_atomic__srv__GetConfig_Response__get_individual_type_description_source(NULL),
    constructed = true;
  }
  return &source_sequence;
}

const rosidl_runtime_c__type_description__TypeSource__Sequence *
robot_param_atomic__srv__GetConfig_Event__get_type_description_sources(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static rosidl_runtime_c__type_description__TypeSource sources[5];
  static const rosidl_runtime_c__type_description__TypeSource__Sequence source_sequence = {sources, 5, 5};
  static bool constructed = false;
  if (!constructed) {
    sources[0] = *robot_param_atomic__srv__GetConfig_Event__get_individual_type_description_source(NULL),
    sources[1] = *builtin_interfaces__msg__Time__get_individual_type_description_source(NULL);
    sources[2] = *robot_param_atomic__srv__GetConfig_Request__get_individual_type_description_source(NULL);
    sources[3] = *robot_param_atomic__srv__GetConfig_Response__get_individual_type_description_source(NULL);
    sources[4] = *service_msgs__msg__ServiceEventInfo__get_individual_type_description_source(NULL);
    constructed = true;
  }
  return &source_sequence;
}
