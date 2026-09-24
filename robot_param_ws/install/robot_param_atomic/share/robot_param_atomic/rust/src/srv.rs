#[cfg(feature = "serde")]
use serde::{Deserialize, Serialize};




// Corresponds to robot_param_atomic__srv__UpdateConfig_Request

// This struct is not documented.
#[allow(missing_docs)]

#[allow(non_camel_case_types)]
#[cfg_attr(feature = "serde", derive(Deserialize, Serialize))]
#[derive(Clone, Debug, PartialEq, PartialOrd)]
pub struct UpdateConfig_Request {

    // This member is not documented.
    #[allow(missing_docs)]
    pub expected_version: f64,


    // This member is not documented.
    #[allow(missing_docs)]
    pub sampling_rate_hz: f64,


    // This member is not documented.
    #[allow(missing_docs)]
    pub cache_length_s: f64,


    // This member is not documented.
    #[allow(missing_docs)]
    pub allowed_latency_s: f64,

}



impl Default for UpdateConfig_Request {
  fn default() -> Self {
    <Self as rosidl_runtime_rs::Message>::from_rmw_message(super::srv::rmw::UpdateConfig_Request::default())
  }
}

impl rosidl_runtime_rs::Message for UpdateConfig_Request {
  type RmwMsg = super::srv::rmw::UpdateConfig_Request;

  fn into_rmw_message(msg_cow: std::borrow::Cow<'_, Self>) -> std::borrow::Cow<'_, Self::RmwMsg> {
    match msg_cow {
      std::borrow::Cow::Owned(msg) => std::borrow::Cow::Owned(Self::RmwMsg {
        expected_version: msg.expected_version,
        sampling_rate_hz: msg.sampling_rate_hz,
        cache_length_s: msg.cache_length_s,
        allowed_latency_s: msg.allowed_latency_s,
      }),
      std::borrow::Cow::Borrowed(msg) => std::borrow::Cow::Owned(Self::RmwMsg {
      expected_version: msg.expected_version,
      sampling_rate_hz: msg.sampling_rate_hz,
      cache_length_s: msg.cache_length_s,
      allowed_latency_s: msg.allowed_latency_s,
      })
    }
  }

  fn from_rmw_message(msg: Self::RmwMsg) -> Self {
    Self {
      expected_version: msg.expected_version,
      sampling_rate_hz: msg.sampling_rate_hz,
      cache_length_s: msg.cache_length_s,
      allowed_latency_s: msg.allowed_latency_s,
    }
  }
}


// Corresponds to robot_param_atomic__srv__UpdateConfig_Response

// This struct is not documented.
#[allow(missing_docs)]

#[allow(non_camel_case_types)]
#[cfg_attr(feature = "serde", derive(Deserialize, Serialize))]
#[derive(Clone, Debug, PartialEq, PartialOrd)]
pub struct UpdateConfig_Response {

    // This member is not documented.
    #[allow(missing_docs)]
    pub ok: bool,


    // This member is not documented.
    #[allow(missing_docs)]
    pub code: u8,


    // This member is not documented.
    #[allow(missing_docs)]
    pub message: std::string::String,

    /// Committed snapshot after the attempt (unchanged on rejection).
    pub version: f64,


    // This member is not documented.
    #[allow(missing_docs)]
    pub sampling_rate_hz: f64,


    // This member is not documented.
    #[allow(missing_docs)]
    pub cache_length_s: f64,


    // This member is not documented.
    #[allow(missing_docs)]
    pub allowed_latency_s: f64,

}

impl UpdateConfig_Response {

    // This constant is not documented.
    #[allow(missing_docs)]
    pub const OK: u8 = 0;

    /// per-field or cross-field validation failed
    pub const REJECT_CONSTRAINT: u8 = 1;

    /// expected_version != current version
    pub const REJECT_STALE_VERSION: u8 = 2;

    /// commit attempted from inside a change callback
    pub const REJECT_UPDATE_IN_CALLBACK: u8 = 3;

    /// SQLite write failed / rolled back
    pub const ERR_PERSISTENCE: u8 = 4;


    // This constant is not documented.
    #[allow(missing_docs)]
    pub const ERR_INTERNAL: u8 = 5;

}


impl Default for UpdateConfig_Response {
  fn default() -> Self {
    <Self as rosidl_runtime_rs::Message>::from_rmw_message(super::srv::rmw::UpdateConfig_Response::default())
  }
}

impl rosidl_runtime_rs::Message for UpdateConfig_Response {
  type RmwMsg = super::srv::rmw::UpdateConfig_Response;

  fn into_rmw_message(msg_cow: std::borrow::Cow<'_, Self>) -> std::borrow::Cow<'_, Self::RmwMsg> {
    match msg_cow {
      std::borrow::Cow::Owned(msg) => std::borrow::Cow::Owned(Self::RmwMsg {
        ok: msg.ok,
        code: msg.code,
        message: msg.message.as_str().into(),
        version: msg.version,
        sampling_rate_hz: msg.sampling_rate_hz,
        cache_length_s: msg.cache_length_s,
        allowed_latency_s: msg.allowed_latency_s,
      }),
      std::borrow::Cow::Borrowed(msg) => std::borrow::Cow::Owned(Self::RmwMsg {
      ok: msg.ok,
      code: msg.code,
        message: msg.message.as_str().into(),
      version: msg.version,
      sampling_rate_hz: msg.sampling_rate_hz,
      cache_length_s: msg.cache_length_s,
      allowed_latency_s: msg.allowed_latency_s,
      })
    }
  }

  fn from_rmw_message(msg: Self::RmwMsg) -> Self {
    Self {
      ok: msg.ok,
      code: msg.code,
      message: msg.message.to_string(),
      version: msg.version,
      sampling_rate_hz: msg.sampling_rate_hz,
      cache_length_s: msg.cache_length_s,
      allowed_latency_s: msg.allowed_latency_s,
    }
  }
}


// Corresponds to robot_param_atomic__srv__GetConfig_Request

// This struct is not documented.
#[allow(missing_docs)]

#[allow(non_camel_case_types)]
#[cfg_attr(feature = "serde", derive(Deserialize, Serialize))]
#[derive(Clone, Debug, PartialEq, PartialOrd)]
pub struct GetConfig_Request {

    // This member is not documented.
    #[allow(missing_docs)]
    pub structure_needs_at_least_one_member: u8,

}



impl Default for GetConfig_Request {
  fn default() -> Self {
    <Self as rosidl_runtime_rs::Message>::from_rmw_message(super::srv::rmw::GetConfig_Request::default())
  }
}

impl rosidl_runtime_rs::Message for GetConfig_Request {
  type RmwMsg = super::srv::rmw::GetConfig_Request;

  fn into_rmw_message(msg_cow: std::borrow::Cow<'_, Self>) -> std::borrow::Cow<'_, Self::RmwMsg> {
    match msg_cow {
      std::borrow::Cow::Owned(msg) => std::borrow::Cow::Owned(Self::RmwMsg {
        structure_needs_at_least_one_member: msg.structure_needs_at_least_one_member,
      }),
      std::borrow::Cow::Borrowed(msg) => std::borrow::Cow::Owned(Self::RmwMsg {
      structure_needs_at_least_one_member: msg.structure_needs_at_least_one_member,
      })
    }
  }

  fn from_rmw_message(msg: Self::RmwMsg) -> Self {
    Self {
      structure_needs_at_least_one_member: msg.structure_needs_at_least_one_member,
    }
  }
}


// Corresponds to robot_param_atomic__srv__GetConfig_Response

// This struct is not documented.
#[allow(missing_docs)]

#[allow(non_camel_case_types)]
#[cfg_attr(feature = "serde", derive(Deserialize, Serialize))]
#[derive(Clone, Debug, PartialEq, PartialOrd)]
pub struct GetConfig_Response {

    // This member is not documented.
    #[allow(missing_docs)]
    pub version: f64,


    // This member is not documented.
    #[allow(missing_docs)]
    pub sampling_rate_hz: f64,


    // This member is not documented.
    #[allow(missing_docs)]
    pub cache_length_s: f64,


    // This member is not documented.
    #[allow(missing_docs)]
    pub allowed_latency_s: f64,

    /// SHA-256 hex digest of the canonical JSON snapshot that was persisted.
    pub config_hash: std::string::String,

    /// Monotonic sequence incremented for every successfully committed update.
    pub commit_seq: i64,

}



impl Default for GetConfig_Response {
  fn default() -> Self {
    <Self as rosidl_runtime_rs::Message>::from_rmw_message(super::srv::rmw::GetConfig_Response::default())
  }
}

impl rosidl_runtime_rs::Message for GetConfig_Response {
  type RmwMsg = super::srv::rmw::GetConfig_Response;

  fn into_rmw_message(msg_cow: std::borrow::Cow<'_, Self>) -> std::borrow::Cow<'_, Self::RmwMsg> {
    match msg_cow {
      std::borrow::Cow::Owned(msg) => std::borrow::Cow::Owned(Self::RmwMsg {
        version: msg.version,
        sampling_rate_hz: msg.sampling_rate_hz,
        cache_length_s: msg.cache_length_s,
        allowed_latency_s: msg.allowed_latency_s,
        config_hash: msg.config_hash.as_str().into(),
        commit_seq: msg.commit_seq,
      }),
      std::borrow::Cow::Borrowed(msg) => std::borrow::Cow::Owned(Self::RmwMsg {
      version: msg.version,
      sampling_rate_hz: msg.sampling_rate_hz,
      cache_length_s: msg.cache_length_s,
      allowed_latency_s: msg.allowed_latency_s,
        config_hash: msg.config_hash.as_str().into(),
      commit_seq: msg.commit_seq,
      })
    }
  }

  fn from_rmw_message(msg: Self::RmwMsg) -> Self {
    Self {
      version: msg.version,
      sampling_rate_hz: msg.sampling_rate_hz,
      cache_length_s: msg.cache_length_s,
      allowed_latency_s: msg.allowed_latency_s,
      config_hash: msg.config_hash.to_string(),
      commit_seq: msg.commit_seq,
    }
  }
}






#[link(name = "robot_param_atomic__rosidl_typesupport_c")]
extern "C" {
    fn rosidl_typesupport_c__get_service_type_support_handle__robot_param_atomic__srv__UpdateConfig() -> *const std::ffi::c_void;
}

// Corresponds to robot_param_atomic__srv__UpdateConfig
#[allow(missing_docs, non_camel_case_types)]
pub struct UpdateConfig;

impl rosidl_runtime_rs::Service for UpdateConfig {
    type Request = UpdateConfig_Request;
    type Response = UpdateConfig_Response;

    fn get_type_support() -> *const std::ffi::c_void {
        // SAFETY: No preconditions for this function.
        unsafe { rosidl_typesupport_c__get_service_type_support_handle__robot_param_atomic__srv__UpdateConfig() }
    }
}




#[link(name = "robot_param_atomic__rosidl_typesupport_c")]
extern "C" {
    fn rosidl_typesupport_c__get_service_type_support_handle__robot_param_atomic__srv__GetConfig() -> *const std::ffi::c_void;
}

// Corresponds to robot_param_atomic__srv__GetConfig
#[allow(missing_docs, non_camel_case_types)]
pub struct GetConfig;

impl rosidl_runtime_rs::Service for GetConfig {
    type Request = GetConfig_Request;
    type Response = GetConfig_Response;

    fn get_type_support() -> *const std::ffi::c_void {
        // SAFETY: No preconditions for this function.
        unsafe { rosidl_typesupport_c__get_service_type_support_handle__robot_param_atomic__srv__GetConfig() }
    }
}


