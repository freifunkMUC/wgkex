load("@rules_python//python:pip.bzl", "compile_pip_requirements")

compile_pip_requirements(
    name = "requirements",
    src = "//:requirements.txt",
    requirements_txt = "requirements_lock.txt",
    visibility = ["//visibility:public"],
)
