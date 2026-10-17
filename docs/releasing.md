# Releasing to GitHub and PyPI

English | [日本語](ja/releasing.md)

[README](../README.md) · [Setup](setup.md)

The distribution name is `rpi-cxd56xx-gnss`; the Python import name is
`cxd56xx_gnss`. Publish source distributions (sdists) so users can build the
C extension on their Raspberry Pi with their GPIO settings. System dependencies
such as libgpiod are installed separately through the [setup guide](setup.md).
The English README is also the package description on PyPI.

On pushes to `main` and pull requests, **Build and publish** creates an sdist,
checks its metadata, builds a wheel and checks imports in Debian Trixie with
Python 3.13 and libgpiod 2.x. It does not open a GNSS device. Uploading to a
package index requires a manual workflow run.

## Publish a version

1. Set `version` in `pyproject.toml`, commit and push. The initial version is `0.1.0`.
2. Tag a commit whose GitHub Actions build passed, matching the package version:

   ```sh
   git tag v0.1.0
   git push origin v0.1.0
   ```

3. Open **Actions → Build and publish → Run workflow**, select tag `v0.1.0`,
   and choose `testpypi` for `repository`. Manual runs from branches are rejected.
4. Check the TestPyPI description, installation and reception on real hardware.
5. Run the workflow again from the same tag, choosing `pypi` for production.

Only `dist/*.tar.gz` is uploaded. The CI wheel is used for import checks and
is not published. Existing distribution files cannot be overwritten; use a
new version and tag when publishing a correction.

## Test installation from TestPyPI

Prepare the system dependencies, wiring and 400 kHz I2C settings using the
[setup guide](setup.md), then create a separate virtual environment.
Install build dependencies from regular PyPI before using TestPyPI:

```sh
python3 -m venv .venv-testpypi
. .venv-testpypi/bin/activate
python -m pip install --upgrade pip "setuptools>=77" "Cython>=3.0"
CXD56XX_GPIO_CHIP=/dev/gpiochip0 python -m pip install \
  --index-url https://test.pypi.org/simple/ \
  --no-deps --no-build-isolation --no-cache-dir \
  rpi-cxd56xx-gnss==0.1.0
cxd56xx-gnss
```

Adjust the GPIO chip and version to match the hardware and release. After
production publication, users can install in their normal virtual environment:

```sh
CXD56XX_GPIO_CHIP=/dev/gpiochip0 python -m pip install --no-cache-dir rpi-cxd56xx-gnss
```
