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
        requirement("waitress"),
        "//wgkex/config:config",
    ],
)
