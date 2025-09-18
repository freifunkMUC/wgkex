load("@rules_python//python:defs.bzl", "py_binary", "py_test")
load("@pip//:requirements.bzl", "requirement")

py_library(
    name = "metrics",
    srcs = ["metrics.py"],
    visibility = ["//visibility:public"],
    deps = [
       "//wgkex/common:mqtt",
       "//wgkex/common:logger",
       "//wgkex/config:config",
    ],
)

py_test(
    name="metrics_test",
    srcs=["metrics_test.py"],
    deps = [
       "//wgkex/broker:metrics",
       requirement("mock"),
    ],
    size="small",
)

py_binary(
    name="app",
    srcs=["app.py"],
    data=["templates/index.html"],
    visibility=["//visibility:public"],
    deps=[
        requirement("flask"),
        requirement("flask-mqtt"),
        requirement("waitress"),
        "//wgkex/config:config",
        "//wgkex/common:mqtt",
        ":metrics"
    ],
)
