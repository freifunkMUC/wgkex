load("@rules_python//python:defs.bzl", "py_binary", "py_test")
load("@pip//:requirements.bzl", "requirement")

py_library(
    name = "ipam",
    srcs = ["ipam.py", "ipam_json.py", "ipam_netbox.py"],
    visibility = ["//visibility:public"],
    deps = [
       "//wgkex/common:logger",
       "//wgkex/config:config",
       requirement("pynetbox"),
    ],
)

py_test(
    name="ipam_netbox_test",
    srcs=["ipam_netbox_test.py"],
    deps = [
       "//wgkex/broker:ipam",
       requirement("mock"),
    ],
    size="small",
)

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

py_library(
    name = "parker",
    srcs = ["parker.py"],
    visibility = ["//visibility:public"],
    deps = [
       "//wgkex/common:utils",
       "//wgkex/config:config",
    ],
)

py_test(
    name="parker_test",
    srcs=["parker_test.py"],
    deps = [
       "//wgkex/broker:app",
       "//wgkex/broker:parker",
       "//wgkex/config:config",
       requirement("mock"),
       requirement("paho-mqtt"),
    ],
    size="small",
)

py_library(
    name = "signer",
    srcs = ["signer.py"],
    visibility = ["//visibility:public"],
    deps = [
       "//wgkex/config:config",
       requirement("ecdsa")
    ],
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
        "//wgkex/broker:ipam",
        "//wgkex/broker:signer",
        "//wgkex/config:config",
        "//wgkex/common:mqtt",
        ":metrics"
    ],
)
