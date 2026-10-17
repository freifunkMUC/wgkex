load("@rules_python//python:defs.bzl", "py_binary", "py_library", "py_test")
load("@pip//:requirements.bzl", "requirement")


py_library(
    name = "utils",
    srcs = ["utils.py"],
    visibility = ["//visibility:public"],
    deps = [
    ],
)

py_test(
    name = "utils_test",
    srcs = ["utils_test.py"],
    deps = [
       "//wgkex/common:utils",
       "//wgkex/config:config",
    ],
    size="small",
)

py_library(
    name = "logger",
    srcs = ["logger.py"],
    visibility = ["//visibility:public"],
    deps = [
       requirement("PyYAML"),
       "//wgkex/config:config",
    ],
)

py_test(
    name = "logger_test",
    srcs = ["logger_test.py"],
    deps = [
       "//wgkex/common:logger",
    ],
    size="small",
)

py_library(
    name = "mqtt",
    srcs = ["mqtt.py"],
    visibility = ["//visibility:public"]
)

py_library(
    name = "keylist",
    srcs = ["keylist.py"],
    visibility = ["//visibility:public"],
    deps = [
       "//wgkex/common:logger",
    ],
)

py_test(
    name = "keylist_test",
    srcs = ["keylist_test.py"],
    deps = [
       "//wgkex/common:keylist",
    ],
    size="small",
)
