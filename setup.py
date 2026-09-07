# SPDX-License-Identifier: Apache-2.0
import json
import os
import sys

from Cython.Build import cythonize
from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext
from setuptools.errors import PlatformError

define_macros = [("CXD5610_NO_MAIN", "1")]
gpio_chip = os.environ.get("CXD56XX_GPIO_CHIP")
if gpio_chip is not None:
    define_macros.append(("CXD5610_GPIO_CHIP_DEV", json.dumps(gpio_chip)))


class LinuxBuildExt(build_ext):
    def run(self):
        if sys.platform != "linux":
            raise PlatformError(
                "The driver requires Linux i2c-dev and libgpiod >= 2.0. "
                "Build and install it on the target Linux system."
            )
        # Compiler options can change without source timestamps changing.
        self.force = True
        super().run()


setup(
    ext_modules=cythonize(
        [
            Extension(
                "cxd56xx_gnss._cxd5610",
                sources=[
                    "src/cxd56xx_gnss/_cxd5610.pyx",
                    "src/cxd56xx_gnss/cxd5610_rpi.c",
                ],
                include_dirs=["src/cxd56xx_gnss"],
                define_macros=define_macros,
                libraries=["gpiod", "pthread", "m"],
                extra_compile_args=["-pthread"],
            )
        ],
        compiler_directives={"language_level": 3},
        build_dir="build/cython",
    ),
    cmdclass={"build_ext": LinuxBuildExt},
)
