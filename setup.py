from setuptools import setup, find_packages

setup(
    name="wireguard-tools",
    version="0.1.0",
    author="",
    author_email="",
    description="WireGuard Tools",
    license="",
    url="https://github.com/freifunkMUC/wireguard-tools",
    packages=find_packages(exclude="tests"),
    include_package_data=True,
    zip_safe=False,
    install_requires=["Flask", "PyYAML", "voluptuous"],
    setup_requires=["wheel"],
    entry_points={
        "console_scripts": [
            "wgked=wgkex.frontend.wgked:main",
        ],
    },
)
