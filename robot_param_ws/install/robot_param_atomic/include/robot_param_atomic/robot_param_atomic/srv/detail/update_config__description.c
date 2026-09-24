// generated from rosidl_generator_c/resource/idl__description.c.em
// with input from robot_param_atomic:srv/UpdateConfig.idl
// generated code does not contain a copyright notice

#include "robot_param_atomic/srv/detail/update_config__functions.h"

ROSIDL_GENERATOR_C_PUBLIC_robot_param_atomic
const rosidl_type_hash_t *
robot_param_atomic__srv__UpdateConfig__get_type_hash(
  const rosidl_service_type_support_t * type_support)
{
  (void)type_support;
  static rosidl_type_hash_t hash = {1, {
      0x23, 0x8c, 0x59, 0x1d, 0xfd, 0x80, 0xd1, 0xfd,
      0x05, 0x21, 0xe0, 0xb0, 0x5d, 0x2a, 0x7e, 0x1f,
      0x90, 0x88, 0x36, 0x22, 0x49, 0x91, 0x04, 0xf2,
      0x7a, 0x3c, 0xf9, 0xb7, 0xa4, 0xcb, 0x2b, 0x24,
    }};
  return &hash;
}

ROSIDL_GENERATOR_C_PUBLIC_robot_param_atomic
const rosidl_type_hash_t *
robot_param_atomic__srv__UpdateConfig_Request__get_type_hash(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static rosidl_type_hash_t hash = {1, {
      0xba, 0xda, 0x15, 0x32, 0x2a, 0x93, 0x03, 0xa2,
      0xec, 0x43, 0xae, 0xbb, 0xf7, 0xaa, 0xec, 0x4a,
      0x5a, 0x54, 0x07, 0xde, 0xa4, 0xd4, 0x5a, 0x40,
      0x54, 0x36, 0x82, 0x51, 0x69, 0xed, 0xcd, 0x5c,
    }};
  return &hash;
}

ROSIDL_GENERATOR_C_PUBLIC_robot_param_atomic
const rosidl_type_hash_t *
robot_param_atomic__srv__UpdateConfig_Response__get_type_hash(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static rosidl_type_hash_t hash = {1, {
      0x4f, 0xf7, 0x7f, 0x25, 0x76, 0x28, 0xef, 0xa2,
      0xeb, 0x82, 0xd4, 0xec, 0x05, 0x7f, 0xed, 0x0f,
      0x3c, 0x34, 0x7f, 0x03, 0xf4, 0x33, 0x1d, 0xb4,
      0x83, 0x50, 0x6c, 0xf1, 0x23, 0x82, 0xca, 0xe6,
    }};
  return &hash;
}

ROSIDL_GENERATOR_C_PUBLIC_robot_param_atomic
const rosidl_type_hash_t *
robot_param_atomic__srv__UpdateConfig_Event__get_type_hash(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static rosidl_type_hash_t hash = {1, {
      0xc3, 0xa8, 0x1b, 0x40, 0xb8, 0x58, 0x96, 0x08,
      0x60, 0xc6, 0x4c, 0x30, 0x41, 0xe4, 0x41, 0x38,
      0x6b, 0xad, 0x8d, 0xf0, 0x6b, 0xe2, 0xf1, 0x69,
      0xef, 0xd8, 0x1b, 0xfe, 0x62, 0x8b, 0xdd, 0x44,
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

static char robot_param_atomic__srv__UpdateConfig__TYPE_NAME[] = "robot_param_atomic/srv/UpdateConfig";
static char builtin_interfaces__msg__Time__TYPE_NAME[] = "builtin_interfaces/msg/Time";
static char robot_param_atomic__srv__UpdateConfig_Event__TYPE_NAME[] = "robot_param_atomic/srv/UpdateConfig_Event";
static char robot_param_atomic__srv__UpdateConfig_Request__TYPE_NAME[] = "robot_param_atomic/srv/UpdateConfig_Request";
static char robot_param_atomic__srv__UpdateConfig_Response__TYPE_NAME[] = "robot_param_atomic/srv/UpdateConfig_Response";
static char service_msgs__msg__ServiceEventInfo__TYPE_NAME[] = "service_msgs/msg/ServiceEventInfo";

// Define type names, field names, and default values
static char robot_param_atomic__srv__UpdateConfig__FIELD_NAME__request_message[] = "request_message";
static char robot_param_atomic__srv__UpdateConfig__FIELD_NAME__response_message[] = "response_message";
static char robot_param_atomic__srv__UpdateConfig__FIELD_NAME__event_message[] = "event_message";

static rosidl_runtime_c__type_description__Field robot_param_atomic__srv__UpdateConfig__FIELDS[] = {
  {
    {robot_param_atomic__srv__UpdateConfig__FIELD_NAME__request_message, 15, 15},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_NESTED_TYPE,
      0,
      0,
      {robot_param_atomic__srv__UpdateConfig_Request__TYPE_NAME, 43, 43},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig__FIELD_NAME__response_message, 16, 16},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_NESTED_TYPE,
      0,
      0,
      {robot_param_atomic__srv__UpdateConfig_Response__TYPE_NAME, 44, 44},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig__FIELD_NAME__event_message, 13, 13},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_NESTED_TYPE,
      0,
      0,
      {robot_param_atomic__srv__UpdateConfig_Event__TYPE_NAME, 41, 41},
    },
    {NULL, 0, 0},
  },
};

static rosidl_runtime_c__type_description__IndividualTypeDescription robot_param_atomic__srv__UpdateConfig__REFERENCED_TYPE_DESCRIPTIONS[] = {
  {
    {builtin_interfaces__msg__Time__TYPE_NAME, 27, 27},
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig_Event__TYPE_NAME, 41, 41},
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig_Request__TYPE_NAME, 43, 43},
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig_Response__TYPE_NAME, 44, 44},
    {NULL, 0, 0},
  },
  {
    {service_msgs__msg__ServiceEventInfo__TYPE_NAME, 33, 33},
    {NULL, 0, 0},
  },
};

const rosidl_runtime_c__type_description__TypeDescription *
robot_param_atomic__srv__UpdateConfig__get_type_description(
  const rosidl_service_type_support_t * type_support)
{
  (void)type_support;
  static bool constructed = false;
  static const rosidl_runtime_c__type_description__TypeDescription description = {
    {
      {robot_param_atomic__srv__UpdateConfig__TYPE_NAME, 35, 35},
      {robot_param_atomic__srv__UpdateConfig__FIELDS, 3, 3},
    },
    {robot_param_atomic__srv__UpdateConfig__REFERENCED_TYPE_DESCRIPTIONS, 5, 5},
  };
  if (!constructed) {
    assert(0 == memcmp(&builtin_interfaces__msg__Time__EXPECTED_HASH, builtin_interfaces__msg__Time__get_type_hash(NULL), sizeof(rosidl_type_hash_t)));
    description.referenced_type_descriptions.data[0].fields = builtin_interfaces__msg__Time__get_type_description(NULL)->type_description.fields;
    description.referenced_type_descriptions.data[1].fields = robot_param_atomic__srv__UpdateConfig_Event__get_type_description(NULL)->type_description.fields;
    description.referenced_type_descriptions.data[2].fields = robot_param_atomic__srv__UpdateConfig_Request__get_type_description(NULL)->type_description.fields;
    description.referenced_type_descriptions.data[3].fields = robot_param_atomic__srv__UpdateConfig_Response__get_type_description(NULL)->type_description.fields;
    assert(0 == memcmp(&service_msgs__msg__ServiceEventInfo__EXPECTED_HASH, service_msgs__msg__ServiceEventInfo__get_type_hash(NULL), sizeof(rosidl_type_hash_t)));
    description.referenced_type_descriptions.data[4].fields = service_msgs__msg__ServiceEventInfo__get_type_description(NULL)->type_description.fields;
    constructed = true;
  }
  return &description;
}
// Define type names, field names, and default values
static char robot_param_atomic__srv__UpdateConfig_Request__FIELD_NAME__expected_version[] = "expected_version";
static char robot_param_atomic__srv__UpdateConfig_Request__FIELD_NAME__sampling_rate_hz[] = "sampling_rate_hz";
static char robot_param_atomic__srv__UpdateConfig_Request__FIELD_NAME__cache_length_s[] = "cache_length_s";
static char robot_param_atomic__srv__UpdateConfig_Request__FIELD_NAME__allowed_latency_s[] = "allowed_latency_s";

static rosidl_runtime_c__type_description__Field robot_param_atomic__srv__UpdateConfig_Request__FIELDS[] = {
  {
    {robot_param_atomic__srv__UpdateConfig_Request__FIELD_NAME__expected_version, 16, 16},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_DOUBLE,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig_Request__FIELD_NAME__sampling_rate_hz, 16, 16},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_DOUBLE,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig_Request__FIELD_NAME__cache_length_s, 14, 14},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_DOUBLE,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig_Request__FIELD_NAME__allowed_latency_s, 17, 17},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_DOUBLE,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
};

const rosidl_runtime_c__type_description__TypeDescription *
robot_param_atomic__srv__UpdateConfig_Request__get_type_description(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static bool constructed = false;
  static const rosidl_runtime_c__type_description__TypeDescription description = {
    {
      {robot_param_atomic__srv__UpdateConfig_Request__TYPE_NAME, 43, 43},
      {robot_param_atomic__srv__UpdateConfig_Request__FIELDS, 4, 4},
    },
    {NULL, 0, 0},
  };
  if (!constructed) {
    constructed = true;
  }
  return &description;
}
// Define type names, field names, and default values
static char robot_param_atomic__srv__UpdateConfig_Response__FIELD_NAME__ok[] = "ok";
static char robot_param_atomic__srv__UpdateConfig_Response__FIELD_NAME__code[] = "code";
static char robot_param_atomic__srv__UpdateConfig_Response__FIELD_NAME__message[] = "message";
static char robot_param_atomic__srv__UpdateConfig_Response__FIELD_NAME__version[] = "version";
static char robot_param_atomic__srv__UpdateConfig_Response__FIELD_NAME__sampling_rate_hz[] = "sampling_rate_hz";
static char robot_param_atomic__srv__UpdateConfig_Response__FIELD_NAME__cache_length_s[] = "cache_length_s";
static char robot_param_atomic__srv__UpdateConfig_Response__FIELD_NAME__allowed_latency_s[] = "allowed_latency_s";

static rosidl_runtime_c__type_description__Field robot_param_atomic__srv__UpdateConfig_Response__FIELDS[] = {
  {
    {robot_param_atomic__srv__UpdateConfig_Response__FIELD_NAME__ok, 2, 2},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_BOOLEAN,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig_Response__FIELD_NAME__code, 4, 4},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_UINT8,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig_Response__FIELD_NAME__message, 7, 7},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_STRING,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig_Response__FIELD_NAME__version, 7, 7},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_DOUBLE,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig_Response__FIELD_NAME__sampling_rate_hz, 16, 16},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_DOUBLE,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig_Response__FIELD_NAME__cache_length_s, 14, 14},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_DOUBLE,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig_Response__FIELD_NAME__allowed_latency_s, 17, 17},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_DOUBLE,
      0,
      0,
      {NULL, 0, 0},
    },
    {NULL, 0, 0},
  },
};

const rosidl_runtime_c__type_description__TypeDescription *
robot_param_atomic__srv__UpdateConfig_Response__get_type_description(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static bool constructed = false;
  static const rosidl_runtime_c__type_description__TypeDescription description = {
    {
      {robot_param_atomic__srv__UpdateConfig_Response__TYPE_NAME, 44, 44},
      {robot_param_atomic__srv__UpdateConfig_Response__FIELDS, 7, 7},
    },
    {NULL, 0, 0},
  };
  if (!constructed) {
    constructed = true;
  }
  return &description;
}
// Define type names, field names, and default values
static char robot_param_atomic__srv__UpdateConfig_Event__FIELD_NAME__info[] = "info";
static char robot_param_atomic__srv__UpdateConfig_Event__FIELD_NAME__request[] = "request";
static char robot_param_atomic__srv__UpdateConfig_Event__FIELD_NAME__response[] = "response";

static rosidl_runtime_c__type_description__Field robot_param_atomic__srv__UpdateConfig_Event__FIELDS[] = {
  {
    {robot_param_atomic__srv__UpdateConfig_Event__FIELD_NAME__info, 4, 4},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_NESTED_TYPE,
      0,
      0,
      {service_msgs__msg__ServiceEventInfo__TYPE_NAME, 33, 33},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig_Event__FIELD_NAME__request, 7, 7},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_NESTED_TYPE_BOUNDED_SEQUENCE,
      1,
      0,
      {robot_param_atomic__srv__UpdateConfig_Request__TYPE_NAME, 43, 43},
    },
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig_Event__FIELD_NAME__response, 8, 8},
    {
      rosidl_runtime_c__type_description__FieldType__FIELD_TYPE_NESTED_TYPE_BOUNDED_SEQUENCE,
      1,
      0,
      {robot_param_atomic__srv__UpdateConfig_Response__TYPE_NAME, 44, 44},
    },
    {NULL, 0, 0},
  },
};

static rosidl_runtime_c__type_description__IndividualTypeDescription robot_param_atomic__srv__UpdateConfig_Event__REFERENCED_TYPE_DESCRIPTIONS[] = {
  {
    {builtin_interfaces__msg__Time__TYPE_NAME, 27, 27},
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig_Request__TYPE_NAME, 43, 43},
    {NULL, 0, 0},
  },
  {
    {robot_param_atomic__srv__UpdateConfig_Response__TYPE_NAME, 44, 44},
    {NULL, 0, 0},
  },
  {
    {service_msgs__msg__ServiceEventInfo__TYPE_NAME, 33, 33},
    {NULL, 0, 0},
  },
};

const rosidl_runtime_c__type_description__TypeDescription *
robot_param_atomic__srv__UpdateConfig_Event__get_type_description(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static bool constructed = false;
  static const rosidl_runtime_c__type_description__TypeDescription description = {
    {
      {robot_param_atomic__srv__UpdateConfig_Event__TYPE_NAME, 41, 41},
      {robot_param_atomic__srv__UpdateConfig_Event__FIELDS, 3, 3},
    },
    {robot_param_atomic__srv__UpdateConfig_Event__REFERENCED_TYPE_DESCRIPTIONS, 4, 4},
  };
  if (!constructed) {
    assert(0 == memcmp(&builtin_interfaces__msg__Time__EXPECTED_HASH, builtin_interfaces__msg__Time__get_type_hash(NULL), sizeof(rosidl_type_hash_t)));
    description.referenced_type_descriptions.data[0].fields = builtin_interfaces__msg__Time__get_type_description(NULL)->type_description.fields;
    description.referenced_type_descriptions.data[1].fields = robot_param_atomic__srv__UpdateConfig_Request__get_type_description(NULL)->type_description.fields;
    description.referenced_type_descriptions.data[2].fields = robot_param_atomic__srv__UpdateConfig_Response__get_type_description(NULL)->type_description.fields;
    assert(0 == memcmp(&service_msgs__msg__ServiceEventInfo__EXPECTED_HASH, service_msgs__msg__ServiceEventInfo__get_type_hash(NULL), sizeof(rosidl_type_hash_t)));
    description.referenced_type_descriptions.data[3].fields = service_msgs__msg__ServiceEventInfo__get_type_description(NULL)->type_description.fields;
    constructed = true;
  }
  return &description;
}

static char toplevel_type_raw_source[] =
  "# Atomically update the robot configuration.\n"
  "#\n"
  "# The three fields are packed into one request so they are validated and\n"
  "# committed as a single snapshot.  expected_version is an optimistic\n"
  "# concurrency token: it must equal the currently committed version, otherwise\n"
  "# the whole request is rejected without touching storage (compare-and-swap).\n"
  "#\n"
  "# Field semantics:\n"
  "#   sampling_rate_hz  sampling rate in hertz            (> 0, finite)\n"
  "#   cache_length_s    cache window length in seconds    (>= 0, finite)\n"
  "#   allowed_latency_s tolerated latency in seconds       (>= 0, finite)\n"
  "# Cross-field invariant (evaluated on the COMPLETE candidate snapshot):\n"
  "#   cache_length_s >= 2 * allowed_latency_s\n"
  "#\n"
  "# A sentinel value of -1.0 on any field means \"leave this field unchanged\";\n"
  "# callers can therefore send a partial update but the server still validates\n"
  "# the resulting whole snapshot.\n"
  "\n"
  "float64 expected_version\n"
  "float64 sampling_rate_hz\n"
  "float64 cache_length_s\n"
  "float64 allowed_latency_s\n"
  "---\n"
  "bool ok\n"
  "uint8 code\n"
  "string message\n"
  "# Committed snapshot after the attempt (unchanged on rejection).\n"
  "float64 version\n"
  "float64 sampling_rate_hz\n"
  "float64 cache_length_s\n"
  "float64 allowed_latency_s\n"
  "\n"
  "uint8 OK                        = 0\n"
  "uint8 REJECT_CONSTRAINT         = 1   # per-field or cross-field validation failed\n"
  "uint8 REJECT_STALE_VERSION      = 2   # expected_version != current version\n"
  "uint8 REJECT_UPDATE_IN_CALLBACK = 3   # commit attempted from inside a change callback\n"
  "uint8 ERR_PERSISTENCE           = 4   # SQLite write failed / rolled back\n"
  "uint8 ERR_INTERNAL              = 5";

static char srv_encoding[] = "srv";
static char implicit_encoding[] = "implicit";

// Define all individual source functions

const rosidl_runtime_c__type_description__TypeSource *
robot_param_atomic__srv__UpdateConfig__get_individual_type_description_source(
  const rosidl_service_type_support_t * type_support)
{
  (void)type_support;
  static const rosidl_runtime_c__type_description__TypeSource source = {
    {robot_param_atomic__srv__UpdateConfig__TYPE_NAME, 35, 35},
    {srv_encoding, 3, 3},
    {toplevel_type_raw_source, 1567, 1567},
  };
  return &source;
}

const rosidl_runtime_c__type_description__TypeSource *
robot_param_atomic__srv__UpdateConfig_Request__get_individual_type_description_source(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static const rosidl_runtime_c__type_description__TypeSource source = {
    {robot_param_atomic__srv__UpdateConfig_Request__TYPE_NAME, 43, 43},
    {implicit_encoding, 8, 8},
    {NULL, 0, 0},
  };
  return &source;
}

const rosidl_runtime_c__type_description__TypeSource *
robot_param_atomic__srv__UpdateConfig_Response__get_individual_type_description_source(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static const rosidl_runtime_c__type_description__TypeSource source = {
    {robot_param_atomic__srv__UpdateConfig_Response__TYPE_NAME, 44, 44},
    {implicit_encoding, 8, 8},
    {NULL, 0, 0},
  };
  return &source;
}

const rosidl_runtime_c__type_description__TypeSource *
robot_param_atomic__srv__UpdateConfig_Event__get_individual_type_description_source(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static const rosidl_runtime_c__type_description__TypeSource source = {
    {robot_param_atomic__srv__UpdateConfig_Event__TYPE_NAME, 41, 41},
    {implicit_encoding, 8, 8},
    {NULL, 0, 0},
  };
  return &source;
}

const rosidl_runtime_c__type_description__TypeSource__Sequence *
robot_param_atomic__srv__UpdateConfig__get_type_description_sources(
  const rosidl_service_type_support_t * type_support)
{
  (void)type_support;
  static rosidl_runtime_c__type_description__TypeSource sources[6];
  static const rosidl_runtime_c__type_description__TypeSource__Sequence source_sequence = {sources, 6, 6};
  static bool constructed = false;
  if (!constructed) {
    sources[0] = *robot_param_atomic__srv__UpdateConfig__get_individual_type_description_source(NULL),
    sources[1] = *builtin_interfaces__msg__Time__get_individual_type_description_source(NULL);
    sources[2] = *robot_param_atomic__srv__UpdateConfig_Event__get_individual_type_description_source(NULL);
    sources[3] = *robot_param_atomic__srv__UpdateConfig_Request__get_individual_type_description_source(NULL);
    sources[4] = *robot_param_atomic__srv__UpdateConfig_Response__get_individual_type_description_source(NULL);
    sources[5] = *service_msgs__msg__ServiceEventInfo__get_individual_type_description_source(NULL);
    constructed = true;
  }
  return &source_sequence;
}

const rosidl_runtime_c__type_description__TypeSource__Sequence *
robot_param_atomic__srv__UpdateConfig_Request__get_type_description_sources(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static rosidl_runtime_c__type_description__TypeSource sources[1];
  static const rosidl_runtime_c__type_description__TypeSource__Sequence source_sequence = {sources, 1, 1};
  static bool constructed = false;
  if (!constructed) {
    sources[0] = *robot_param_atomic__srv__UpdateConfig_Request__get_individual_type_description_source(NULL),
    constructed = true;
  }
  return &source_sequence;
}

const rosidl_runtime_c__type_description__TypeSource__Sequence *
robot_param_atomic__srv__UpdateConfig_Response__get_type_description_sources(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static rosidl_runtime_c__type_description__TypeSource sources[1];
  static const rosidl_runtime_c__type_description__TypeSource__Sequence source_sequence = {sources, 1, 1};
  static bool constructed = false;
  if (!constructed) {
    sources[0] = *robot_param_atomic__srv__UpdateConfig_Response__get_individual_type_description_source(NULL),
    constructed = true;
  }
  return &source_sequence;
}

const rosidl_runtime_c__type_description__TypeSource__Sequence *
robot_param_atomic__srv__UpdateConfig_Event__get_type_description_sources(
  const rosidl_message_type_support_t * type_support)
{
  (void)type_support;
  static rosidl_runtime_c__type_description__TypeSource sources[5];
  static const rosidl_runtime_c__type_description__TypeSource__Sequence source_sequence = {sources, 5, 5};
  static bool constructed = false;
  if (!constructed) {
    sources[0] = *robot_param_atomic__srv__UpdateConfig_Event__get_individual_type_description_source(NULL),
    sources[1] = *builtin_interfaces__msg__Time__get_individual_type_description_source(NULL);
    sources[2] = *robot_param_atomic__srv__UpdateConfig_Request__get_individual_type_description_source(NULL);
    sources[3] = *robot_param_atomic__srv__UpdateConfig_Response__get_individual_type_description_source(NULL);
    sources[4] = *service_msgs__msg__ServiceEventInfo__get_individual_type_description_source(NULL);
    constructed = true;
  }
  return &source_sequence;
}
