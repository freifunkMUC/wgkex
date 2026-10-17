load("@rules_python//python:defs.bzl", "py_binary", "py_library", "py_test")
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
    ],
    size="small",
)

py_library(
    name = "assignments",
    srcs = ["assignments.py"],
    visibility = ["//visibility:public"],
)

py_test(
    name="assignments_test",
    srcs=["assignments_test.py"],
    deps = [
       "//wgkex/broker:assignments",
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
        requirement("prometheus-client"),
        requirement("waitress"),
        "//wgkex/config:config",
        "//wgkex/common:keylist",
        "//wgkex/common:logger",
        "//wgkex/common:mqtt",
        "//wgkex/common:utils",
        ":assignments",
        ":metrics",
        ":observability",
        ":ratelimit",
    ],
)

py_library(
    name = "observability",
    srcs = ["observability.py"],
    visibility = ["//visibility:public"],
    deps = [
       requirement("prometheus-client"),
       ":metrics",
    ],
)

py_test(
    name="app_test",
    srcs=["app_test.py"],
    deps=[
        "//wgkex/broker:app",
        "//wgkex/common:mqtt",
        "//wgkex/config:config",
    ],
    size="small",
)

py_library(
    name = "ratelimit",
    srcs = ["ratelimit.py"],
    visibility = ["//visibility:public"],
)

py_test(
    name="ratelimit_test",
    srcs=["ratelimit_test.py"],
    deps = [
       "//wgkex/broker:ratelimit",
    ],
    size="small",
)
