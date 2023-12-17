load("@rules_python//python:defs.bzl", "py_binary", "py_test")
load("@pip//:requirements.bzl", "requirement")


py_library(
    name = "utils",
    srcs = ["utils.py"],
    visibility = ["//visibility:public"],
    deps = [
       requirement("ipaddress"),
    ],
)

py_test(
    name = "utils_test",
    srcs = ["utils_test.py"],
    deps = [
       "//wgkex/common:utils",
       "//wgkex/config:config",
       requirement("mock"),
    ],
)

py_library(
    name = "logger",
    srcs = ["logger.py"],
    visibility = ["//visibility:public"]
)