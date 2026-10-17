load("@rules_python//python:defs.bzl", "py_binary", "py_library", "py_test")
load("@pip//:requirements.bzl", "requirement")


py_library(
    name = "netlink",
    srcs = ["netlink.py"],
    visibility = ["//visibility:public"],
    deps = [
       requirement("paho-mqtt"),
       requirement("pyroute2"),
       "//wgkex/common:utils",
       "//wgkex/common:logger",
       "//wgkex/config:config",
       ":observability",
    ],
)

py_library(
    name = "observability",
    srcs = ["observability.py"],
    visibility = ["//visibility:public"],
    deps = [
       requirement("prometheus-client"),
       "//wgkex/common:logger",
    ],
)

py_test(
    name = "observability_test",
    srcs = ["observability_test.py"],
    deps = [
       ":observability",
    ],
    size="small",
)


py_test(
    name = "netlink_test",
    srcs = ["netlink_test.py"],
    deps = [
       "//wgkex/worker:netlink",
       requirement("pyroute2"),
    ],
    size="small",
)

py_library(
    name = "mqtt",
    srcs = ["mqtt.py"],
    visibility = ["//visibility:public"],
    deps = [
       requirement("paho-mqtt"),
       requirement("pyroute2"),
       "//wgkex/common:logger",
       "//wgkex/common:mqtt",
       "//wgkex/common:utils",
       "//wgkex/config:config",
       ":drain",
       ":msg_queue",
       ":netlink",
       ":rebalance",
    ],
)

py_library(
    name = "drain",
    srcs = ["drain.py"],
    visibility = ["//visibility:public"],
    deps = [
       "//wgkex/common:logger",
       "//wgkex/common:mqtt",
    ],
)

py_test(
    name = "drain_test",
    srcs = ["drain_test.py"],
    deps = [
       ":drain",
    ],
    size="small",
)

py_test(
    name = "mqtt_test",
    srcs = ["mqtt_test.py"],
    deps = [
       "//wgkex/worker:mqtt",
       "//wgkex/worker:msg_queue",
    ],
    size="small",
)

py_binary(
    name = "app",
    srcs = ["app.py"],
    deps = [
       ":allowlist",
       ":drain",
       ":mqtt",
       ":msg_queue",
       ":observability",
       ":rebalance",
       "//wgkex/config:config",
       "//wgkex/common:logger",
    ],
)

py_test(
    name = "app_test",
    srcs = ["app_test.py"],
    deps = [
       "//wgkex/config:config",
       "//wgkex/worker:app",
       "//wgkex/worker:msg_queue",
    ],
    size="small",
)

py_library(
    name = "msg_queue",
    srcs = ["msg_queue.py"],
    visibility = ["//visibility:public"],
    deps = [
       "//wgkex/common:logger",
       ":netlink",
       ":observability",
       ":rebalance",
    ],
)

py_test(
    name = "msg_queue_test",
    srcs = ["msg_queue_test.py"],
    deps = [
       "//wgkex/worker:msg_queue",
    ],
    size="small",
)

py_library(
    name = "rebalance",
    srcs = ["rebalance.py"],
    visibility = ["//visibility:public"],
    deps = [
       "//wgkex/common:logger",
       "//wgkex/config:config",
       ":allowlist",
       ":netlink",
       ":observability",
    ],
)

py_test(
    name = "rebalance_test",
    srcs = ["rebalance_test.py"],
    deps = [
       "//wgkex/config:config",
       "//wgkex/worker:rebalance",
    ],
    size="small",
)

py_library(
    name = "allowlist",
    srcs = ["allowlist.py"],
    visibility = ["//visibility:public"],
    deps = [
       "//wgkex/common:keylist",
       "//wgkex/config:config",
    ],
)

py_test(
    name = "allowlist_test",
    srcs = ["allowlist_test.py"],
    deps = [
       "//wgkex/config:config",
       "//wgkex/worker:allowlist",
    ],
    size="small",
)
