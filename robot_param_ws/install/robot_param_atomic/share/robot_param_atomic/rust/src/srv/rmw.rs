#[cfg(feature = "serde")]
use serde::{Deserialize, Serialize};



#[link(name = "robot_param_atomic__rosidl_typesupport_c")]
extern "C" {
    fn rosidl_typesupport_c__get_message_type_support_handle__robot_param_atomic__srv__UpdateConfig_Request() -> *const std::ffi::c_void;
}

#[link(name = "robot_param_atomic__rosidl_generator_c")]
extern "C" {
    fn robot_param_atomic__srv__UpdateConfig_Request__init(msg: *mut UpdateConfig_Request) -> bool;
    fn robot_param_atomic__srv__UpdateConfig_Request__Sequence__init(seq: *mut rosidl_runtime_rs::Sequence<UpdateConfig_Request>, size: usize) -> bool;
    fn robot_param_atomic__srv__UpdateConfig_Request__Sequence__fini(seq: *mut rosidl_runtime_rs::Sequence<UpdateConfig_Request>);
    fn robot_param_atomic__srv__UpdateConfig_Request__Sequence__copy(in_seq: &rosidl_runtime_rs::Sequence<UpdateConfig_Request>, out_seq: *mut rosidl_runtime_rs::Sequence<UpdateConfig_Request>) -> bool;
}

// Corresponds to robot_param_atomic__srv__UpdateConfig_Request
#[cfg_attr(feature = "serde", derive(Deserialize, Serialize))]


// This struct is not documented.
#[allow(missing_docs)]

#[allow(non_camel_case_types)]
#[repr(C)]
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
    unsafe {
      let mut msg = std::mem::zeroed();
      if !robot_param_atomic__srv__UpdateConfig_Request__init(&mut msg as *mut _) {
        panic!("Call to robot_param_atomic__srv__UpdateConfig_Request__init() failed");
      }
      msg
    }
  }
}

impl rosidl_runtime_rs::SequenceAlloc for UpdateConfig_Request {
  fn sequence_init(seq: &mut rosidl_runtime_rs::Sequence<Self>, size: usize) -> bool {
    // SAFETY: This is safe since the pointer is guaranteed to be valid/initialized.
    unsafe { robot_param_atomic__srv__UpdateConfig_Request__Sequence__init(seq as *mut _, size) }
  }
  fn sequence_fini(seq: &mut rosidl_runtime_rs::Sequence<Self>) {
    // SAFETY: This is safe since the pointer is guaranteed to be valid/initialized.
    unsafe { robot_param_atomic__srv__UpdateConfig_Request__Sequence__fini(seq as *mut _) }
  }
  fn sequence_copy(in_seq: &rosidl_runtime_rs::Sequence<Self>, out_seq: &mut rosidl_runtime_rs::Sequence<Self>) -> bool {
    // SAFETY: This is safe since the pointer is guaranteed to be valid/initialized.
    unsafe { robot_param_atomic__srv__UpdateConfig_Request__Sequence__copy(in_seq, out_seq as *mut _) }
  }
}

impl rosidl_runtime_rs::Message for UpdateConfig_Request {
  type RmwMsg = Self;
  fn into_rmw_message(msg_cow: std::borrow::Cow<'_, Self>) -> std::borrow::Cow<'_, Self::RmwMsg> { msg_cow }
  fn from_rmw_message(msg: Self::RmwMsg) -> Self { msg }
}

impl rosidl_runtime_rs::RmwMessage for UpdateConfig_Request where Self: Sized {
  const TYPE_NAME: &'static str = "robot_param_atomic/srv/UpdateConfig_Request";
  fn get_type_support() -> *const std::ffi::c_void {
    // SAFETY: No preconditions for this function.
    unsafe { rosidl_typesupport_c__get_message_type_support_handle__robot_param_atomic__srv__UpdateConfig_Request() }
  }
}


#[link(name = "robot_param_atomic__rosidl_typesupport_c")]
extern "C" {
    fn rosidl_typesupport_c__get_message_type_support_handle__robot_param_atomic__srv__UpdateConfig_Response() -> *const std::ffi::c_void;
}

#[link(name = "robot_param_atomic__rosidl_generator_c")]
extern "C" {
    fn robot_param_atomic__srv__UpdateConfig_Response__init(msg: *mut UpdateConfig_Response) -> bool;
    fn robot_param_atomic__srv__UpdateConfig_Response__Sequence__init(seq: *mut rosidl_runtime_rs::Sequence<UpdateConfig_Response>, size: usize) -> bool;
    fn robot_param_atomic__srv__UpdateConfig_Response__Sequence__fini(seq: *mut rosidl_runtime_rs::Sequence<UpdateConfig_Response>);
    fn robot_param_atomic__srv__UpdateConfig_Response__Sequence__copy(in_seq: &rosidl_runtime_rs::Sequence<UpdateConfig_Response>, out_seq: *mut rosidl_runtime_rs::Sequence<UpdateConfig_Response>) -> bool;
}

// Corresponds to robot_param_atomic__srv__UpdateConfig_Response
#[cfg_attr(feature = "serde", derive(Deserialize, Serialize))]


// This struct is not documented.
#[allow(missing_docs)]

#[allow(non_camel_case_types)]
#[repr(C)]
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
    pub message: rosidl_runtime_rs::String,

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
    unsafe {
      let mut msg = std::mem::zeroed();
      if !robot_param_atomic__srv__UpdateConfig_Response__init(&mut msg as *mut _) {
        panic!("Call to robot_param_atomic__srv__UpdateConfig_Response__init() failed");
      }
      msg
    }
  }
}

impl rosidl_runtime_rs::SequenceAlloc for UpdateConfig_Response {
  fn sequence_init(seq: &mut rosidl_runtime_rs::Sequence<Self>, size: usize) -> bool {
    // SAFETY: This is safe since the pointer is guaranteed to be valid/initialized.
    unsafe { robot_param_atomic__srv__UpdateConfig_Response__Sequence__init(seq as *mut _, size) }
  }
  fn sequence_fini(seq: &mut rosidl_runtime_rs::Sequence<Self>) {
    // SAFETY: This is safe since the pointer is guaranteed to be valid/initialized.
    unsafe { robot_param_atomic__srv__UpdateConfig_Response__Sequence__fini(seq as *mut _) }
  }
  fn sequence_copy(in_seq: &rosidl_runtime_rs::Sequence<Self>, out_seq: &mut rosidl_runtime_rs::Sequence<Self>) -> bool {
    // SAFETY: This is safe since the pointer is guaranteed to be valid/initialized.
    unsafe { robot_param_atomic__srv__UpdateConfig_Response__Sequence__copy(in_seq, out_seq as *mut _) }
  }
}

impl rosidl_runtime_rs::Message for UpdateConfig_Response {
  type RmwMsg = Self;
  fn into_rmw_message(msg_cow: std::borrow::Cow<'_, Self>) -> std::borrow::Cow<'_, Self::RmwMsg> { msg_cow }
  fn from_rmw_message(msg: Self::RmwMsg) -> Self { msg }
}

impl rosidl_runtime_rs::RmwMessage for UpdateConfig_Response where Self: Sized {
  const TYPE_NAME: &'static str = "robot_param_atomic/srv/UpdateConfig_Response";
  fn get_type_support() -> *const std::ffi::c_void {
    // SAFETY: No preconditions for this function.
    unsafe { rosidl_typesupport_c__get_message_type_support_handle__robot_param_atomic__srv__UpdateConfig_Response() }
  }
}


#[link(name = "robot_param_atomic__rosidl_typesupport_c")]
extern "C" {
    fn rosidl_typesupport_c__get_message_type_support_handle__robot_param_atomic__srv__GetConfig_Request() -> *const std::ffi::c_void;
}

#[link(name = "robot_param_atomic__rosidl_generator_c")]
extern "C" {
    fn robot_param_atomic__srv__GetConfig_Request__init(msg: *mut GetConfig_Request) -> bool;
    fn robot_param_atomic__srv__GetConfig_Request__Sequence__init(seq: *mut rosidl_runtime_rs::Sequence<GetConfig_Request>, size: usize) -> bool;
    fn robot_param_atomic__srv__GetConfig_Request__Sequence__fini(seq: *mut rosidl_runtime_rs::Sequence<GetConfig_Request>);
    fn robot_param_atomic__srv__GetConfig_Request__Sequence__copy(in_seq: &rosidl_runtime_rs::Sequence<GetConfig_Request>, out_seq: *mut rosidl_runtime_rs::Sequence<GetConfig_Request>) -> bool;
}

// Corresponds to robot_param_atomic__srv__GetConfig_Request
#[cfg_attr(feature = "serde", derive(Deserialize, Serialize))]


// This struct is not documented.
#[allow(missing_docs)]

#[allow(non_camel_case_types)]
#[repr(C)]
#[derive(Clone, Debug, PartialEq, PartialOrd)]
pub struct GetConfig_Request {

    // This member is not documented.
    #[allow(missing_docs)]
    pub structure_needs_at_least_one_member: u8,

}



impl Default for GetConfig_Request {
  fn default() -> Self {
    unsafe {
      let mut msg = std::mem::zeroed();
      if !robot_param_atomic__srv__GetConfig_Request__init(&mut msg as *mut _) {
        panic!("Call to robot_param_atomic__srv__GetConfig_Request__init() failed");
      }
      msg
    }
  }
}

impl rosidl_runtime_rs::SequenceAlloc for GetConfig_Request {
  fn sequence_init(seq: &mut rosidl_runtime_rs::Sequence<Self>, size: usize) -> bool {
    // SAFETY: This is safe since the pointer is guaranteed to be valid/initialized.
    unsafe { robot_param_atomic__srv__GetConfig_Request__Sequence__init(seq as *mut _, size) }
  }
  fn sequence_fini(seq: &mut rosidl_runtime_rs::Sequence<Self>) {
    // SAFETY: This is safe since the pointer is guaranteed to be valid/initialized.
    unsafe { robot_param_atomic__srv__GetConfig_Request__Sequence__fini(seq as *mut _) }
  }
  fn sequence_copy(in_seq: &rosidl_runtime_rs::Sequence<Self>, out_seq: &mut rosidl_runtime_rs::Sequence<Self>) -> bool {
    // SAFETY: This is safe since the pointer is guaranteed to be valid/initialized.
    unsafe { robot_param_atomic__srv__GetConfig_Request__Sequence__copy(in_seq, out_seq as *mut _) }
  }
}

impl rosidl_runtime_rs::Message for GetConfig_Request {
  type RmwMsg = Self;
  fn into_rmw_message(msg_cow: std::borrow::Cow<'_, Self>) -> std::borrow::Cow<'_, Self::RmwMsg> { msg_cow }
  fn from_rmw_message(msg: Self::RmwMsg) -> Self { msg }
}

impl rosidl_runtime_rs::RmwMessage for GetConfig_Request where Self: Sized {
  const TYPE_NAME: &'static str = "robot_param_atomic/srv/GetConfig_Request";
  fn get_type_support() -> *const std::ffi::c_void {
    // SAFETY: No preconditions for this function.
    unsafe { rosidl_typesupport_c__get_message_type_support_handle__robot_param_atomic__srv__GetConfig_Request() }
  }
}


#[link(name = "robot_param_atomic__rosidl_typesupport_c")]
extern "C" {
    fn rosidl_typesupport_c__get_message_type_support_handle__robot_param_atomic__srv__GetConfig_Response() -> *const std::ffi::c_void;
}

#[link(name = "robot_param_atomic__rosidl_generator_c")]
extern "C" {
    fn robot_param_atomic__srv__GetConfig_Response__init(msg: *mut GetConfig_Response) -> bool;
    fn robot_param_atomic__srv__GetConfig_Response__Sequence__init(seq: *mut rosidl_runtime_rs::Sequence<GetConfig_Response>, size: usize) -> bool;
    fn robot_param_atomic__srv__GetConfig_Response__Sequence__fini(seq: *mut rosidl_runtime_rs::Sequence<GetConfig_Response>);
    fn robot_param_atomic__srv__GetConfig_Response__Sequence__copy(in_seq: &rosidl_runtime_rs::Sequence<GetConfig_Response>, out_seq: *mut rosidl_runtime_rs::Sequence<GetConfig_Response>) -> bool;
}

// Corresponds to robot_param_atomic__srv__GetConfig_Response
#[cfg_attr(feature = "serde", derive(Deserialize, Serialize))]


// This struct is not documented.
#[allow(missing_docs)]

#[allow(non_camel_case_types)]
#[repr(C)]
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
    pub config_hash: rosidl_runtime_rs::String,

    /// Monotonic sequence incremented for every successfully committed update.
    pub commit_seq: i64,

}



impl Default for GetConfig_Response {
  fn default() -> Self {
    unsafe {
      let mut msg = std::mem::zeroed();
      if !robot_param_atomic__srv__GetConfig_Response__init(&mut msg as *mut _) {
        panic!("Call to robot_param_atomic__srv__GetConfig_Response__init() failed");
      }
      msg
    }
  }
}

impl rosidl_runtime_rs::SequenceAlloc for GetConfig_Response {
  fn sequence_init(seq: &mut rosidl_runtime_rs::Sequence<Self>, size: usize) -> bool {
    // SAFETY: This is safe since the pointer is guaranteed to be valid/initialized.
    unsafe { robot_param_atomic__srv__GetConfig_Response__Sequence__init(seq as *mut _, size) }
  }
  fn sequence_fini(seq: &mut rosidl_runtime_rs::Sequence<Self>) {
    // SAFETY: This is safe since the pointer is guaranteed to be valid/initialized.
    unsafe { robot_param_atomic__srv__GetConfig_Response__Sequence__fini(seq as *mut _) }
  }
  fn sequence_copy(in_seq: &rosidl_runtime_rs::Sequence<Self>, out_seq: &mut rosidl_runtime_rs::Sequence<Self>) -> bool {
    // SAFETY: This is safe since the pointer is guaranteed to be valid/initialized.
    unsafe { robot_param_atomic__srv__GetConfig_Response__Sequence__copy(in_seq, out_seq as *mut _) }
  }
}

impl rosidl_runtime_rs::Message for GetConfig_Response {
  type RmwMsg = Self;
  fn into_rmw_message(msg_cow: std::borrow::Cow<'_, Self>) -> std::borrow::Cow<'_, Self::RmwMsg> { msg_cow }
  fn from_rmw_message(msg: Self::RmwMsg) -> Self { msg }
}

impl rosidl_runtime_rs::RmwMessage for GetConfig_Response where Self: Sized {
  const TYPE_NAME: &'static str = "robot_param_atomic/srv/GetConfig_Response";
  fn get_type_support() -> *const std::ffi::c_void {
    // SAFETY: No preconditions for this function.
    unsafe { rosidl_typesupport_c__get_message_type_support_handle__robot_param_atomic__srv__GetConfig_Response() }
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


