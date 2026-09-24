#!/usr/bin/env bash
# Install (and, when PIN=1, strictly pin) all build/test/runtime dependencies.
# Requires an Ubuntu 24.04 system with packages.ros.org configured for Jazzy
# (see https://docs.ros.org/en/jazzy/Installation/Ubuntu-Install-Debs.html).
set -o pipefail

PIN="${PIN:-1}"

sudo apt-get update

install_pinned() {
  local pkg="$1" ver="$2"
  if [ "$PIN" = "1" ]; then
    echo ">> pinning $pkg=$ver"
    sudo apt-get install -y --allow-downgrades "$pkg=$ver"
    sudo apt-mark hold "$pkg"
  else
    sudo apt-get install -y "$pkg"
  fi
}

# Toolchain
install_pinned build-essential 12.10ubuntu1
install_pinned gcc 4:13.2.0-7ubuntu1
install_pinned g++ 4:13.2.0-7ubuntu1
install_pinned cmake 3.28.3-1build7
sudo apt-get install -y python3-colcon-common-extensions python3-pip sqlite3

# ROS 2 Jazzy core packages
install_pinned ros-jazzy-rclcpp 28.1.22-2noble.20260902.055335
install_pinned ros-jazzy-ament-cmake 2.5.6-2noble.20260225.222913
install_pinned ros-jazzy-ament-cmake-gtest 2.5.6-2noble.20260226.145055
install_pinned ros-jazzy-rosidl-default-generators 1.6.1-2noble.20260902.021422
install_pinned ros-jazzy-rosidl-default-runtime 1.6.1-2noble.20260902.015921
install_pinned ros-jazzy-builtin-interfaces 2.0.4-2noble.20260902.013948

# Native libraries
install_pinned libsqlite3-dev 3.45.1-1ubuntu2.8
install_pinned libssl-dev 3.0.13-0ubuntu3.15

# Test dependencies
install_pinned libgtest-dev 1.14.0-1

echo
echo "Dependencies installed (PIN=$PIN). Verify with:"
echo "  dpkg -l | grep -E 'ros-jazzy-(rclcpp|ament-cmake|rosidl|builtin)|libsqlite3-dev|libssl-dev'"
