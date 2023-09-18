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
       "//wgkex/common:utils",
       "//wgkex/common:logger",
       "//wgkex/config:config",
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
       "//wgkex/common:logger",
       "//wgkex/config:config",
       ":msg_queue",
       ":netlink",
    ],
)

py_test(
    name = "mqtt_test",
    srcs = ["mqtt_test.py"],
    deps = [
       ":mqtt",
       ":msg_queue",
       requirement("mock"),
    ],
)

py_binary(
    name = "app",
    srcs = ["app.py"],
    deps = [
       ":mqtt",
       ":msg_queue",
       "//wgkex/config:config",
       "//wgkex/common:logger",
    ],
)

py_test(
    name = "app_test",
    srcs = ["app_test.py"],
    deps = [
       ":app",
       ":msg_queue",
       requirement("mock"),
    ],
)

py_library(
    name = "msg_queue",
    srcs = ["msg_queue.py"],
    visibility = ["//visibility:public"],
    deps = [
       "//wgkex/common:logger",
    ],
)