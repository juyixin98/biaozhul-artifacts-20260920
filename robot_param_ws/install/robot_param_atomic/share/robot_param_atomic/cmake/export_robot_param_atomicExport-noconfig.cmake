#----------------------------------------------------------------
# Generated CMake target import file.
#----------------------------------------------------------------

# Commands may need to know the format version.
set(CMAKE_IMPORT_FILE_VERSION 1)

# Import target "robot_param_atomic::robot_param_atomic_core" for configuration ""
set_property(TARGET robot_param_atomic::robot_param_atomic_core APPEND PROPERTY IMPORTED_CONFIGURATIONS NOCONFIG)
set_target_properties(robot_param_atomic::robot_param_atomic_core PROPERTIES
  IMPORTED_LOCATION_NOCONFIG "${_IMPORT_PREFIX}/lib/librobot_param_atomic_core.so"
  IMPORTED_SONAME_NOCONFIG "librobot_param_atomic_core.so"
  )

list(APPEND _cmake_import_check_targets robot_param_atomic::robot_param_atomic_core )
list(APPEND _cmake_import_check_files_for_robot_param_atomic::robot_param_atomic_core "${_IMPORT_PREFIX}/lib/librobot_param_atomic_core.so" )

# Import target "robot_param_atomic::robot_config_node" for configuration ""
set_property(TARGET robot_param_atomic::robot_config_node APPEND PROPERTY IMPORTED_CONFIGURATIONS NOCONFIG)
set_target_properties(robot_param_atomic::robot_config_node PROPERTIES
  IMPORTED_LOCATION_NOCONFIG "${_IMPORT_PREFIX}/lib/robot_param_atomic/robot_config_node"
  )

list(APPEND _cmake_import_check_targets robot_param_atomic::robot_config_node )
list(APPEND _cmake_import_check_files_for_robot_param_atomic::robot_config_node "${_IMPORT_PREFIX}/lib/robot_param_atomic/robot_config_node" )

# Import target "robot_param_atomic::robot_config_client" for configuration ""
set_property(TARGET robot_param_atomic::robot_config_client APPEND PROPERTY IMPORTED_CONFIGURATIONS NOCONFIG)
set_target_properties(robot_param_atomic::robot_config_client PROPERTIES
  IMPORTED_LOCATION_NOCONFIG "${_IMPORT_PREFIX}/lib/robot_param_atomic/robot_config_client"
  )

list(APPEND _cmake_import_check_targets robot_param_atomic::robot_config_client )
list(APPEND _cmake_import_check_files_for_robot_param_atomic::robot_config_client "${_IMPORT_PREFIX}/lib/robot_param_atomic/robot_config_client" )

# Commands beyond this point should not need to know the version.
set(CMAKE_IMPORT_FILE_VERSION)
