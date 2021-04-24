load("@rules_python//python:defs.bzl", "py_binary", "py_test")
load("@pip//:requirements.bzl", "requirement")


py_library(
    name = "netlink",
    srcs = ["netlink.py"],
    visibility = ["//visibility:public"],
    deps = [
       requirement("NetLink"),
       requirement("paho-mqtt"),
       requirement("pyroute2"),
       "//wgkex/common:utils"
    ],
)

py_test(
    name = "netlink_test",
    srcs = ["netlink_test.py"],
    deps = [
       ":netlink",
       requirement("mock"),
    ],
)

py_library(
    name = "mqtt",
    srcs = ["mqtt.py"],
    visibility = ["//visibility:public"],
    deps = [
       requirement("NetLink"),
       requirement("paho-mqtt"),
       requirement("pyroute2"),
       "//wgkex/common:utils",
       "//wgkex/config:config",
       ":netlink",
    ],
)

py_test(
    name = "mqtt_test",
    srcs = ["mqtt_test.py"],
    deps = [
       ":mqtt",
       requirement("mock"),
    ],
)
