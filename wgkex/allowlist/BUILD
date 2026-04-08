load("@rules_python//python:defs.bzl", "py_library", "py_test")
load("@pip//:requirements.bzl", "requirement")

py_library(
    name = "allowlist",
    srcs = ["allowlist.py"],
    visibility = ["//visibility:public"],
    deps = [
        requirement("PyYAML"),
        "//wgkex/common:logger",
    ],
)

py_test(
    name = "allowlist_test",
    srcs = ["allowlist_test.py"],
    deps = [
        ":allowlist",
        requirement("PyYAML"),
        requirement("mock"),
    ],
    size = "small",
)
