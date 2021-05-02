load("@rules_python//python:defs.bzl", "py_binary", "py_test")
load("@pip//:requirements.bzl", "requirement")

py_binary(
    name="app",
    srcs=["app.py"],
    data=["templates/index.html"],
    visibility=["//visibility:public"],
    deps=[
        requirement("flask"),
        requirement("flask-mqtt"),
        "//wgkex/config:config",
    ],
)

py_test(
    name="app_test",
    srcs=["app_test.py"],
    deps=[
        ":app",
        requirement("mock"),
    ],
)
