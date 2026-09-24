cmake_minimum_required(VERSION 3.8)
project(action_cancel_server)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(action_cancel_interfaces REQUIRED)

ament_python_install_package(action_cancel_server)

install(PROGRAMS
  scripts/segment_server.py
  DESTINATION lib/${PROJECT_NAME}
  RENAME segment_server
)

install(PROGRAMS
  scripts/segment_client.py
  DESTINATION lib/${PROJECT_NAME}
  RENAME segment_client
)

if(BUILD_TESTING)
  find_package(ament_cmake_pytest REQUIRED)
  ament_add_pytest_test(unit_tests
    test
    WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  )
endif()

ament_package()
