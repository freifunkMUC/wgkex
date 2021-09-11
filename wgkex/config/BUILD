load("@rules_python//python:defs.bzl", "py_binary", "py_test")
load("@pip//:requirements.bzl", "requirement")


py_library(
    name="config",
    srcs=["config.py"],
    visibility=["//visibility:public"],
    deps=[requirement("PyYAML"),
    "//wgkex/common:utils",
    "//wgkex/common:logger",
    ],
)

py_test(
    name="config_test",
    srcs=["config_test.py"],
    deps=[
        ":config",
        requirement("mock"),
    ],
)
