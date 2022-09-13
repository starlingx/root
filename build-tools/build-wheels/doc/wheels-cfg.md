# Syntax of wheels config files

The files {debian,centos}/{stable,dev}-wheels.cfg list the 3rd-party wheels
(ie compiled python modules) to be included in the wheels tarball. Wheels are
listed one per line, each with the following "|"-separated fields.

The first field is the wheel file name and is often python version & ABI
specific, eg: `lz4-0.9.0-cp39-cp39-linux_x86_64.whl`.

The second field is `git`, `tar`, `zip`, or `pypi` and determines how to build
or download the wheel

The third field is the URL of wheel source of the wheel itself

The optional last field may be set to `fix_setup`, which will update
older/legacy module sources to use setuptools.setup, which is necessary for
building the module into a wheel. See script
`docker-common/docker-build-wheel.sh` for details.

The exact number of fields depend on the wheel source:

* git: `wheelname|git|repo-url|basedir|branch|fix_setup`

  Remote git repo URL + branch

  Example:

  ```
  lz4-0.9.0-cp39-cp39-linux_x86_64.whl|git|https://github.com/python-lz4/python-lz4|python-lz4|v0.9.0
  ```

* tar: `wheelname|tar|url|basedir|fix_setup`

  Source tarball URL. Basedir must be the subdirectory containing the package
  source code within the tarball, and typically equals the basename of the
  tarfile and/or module name & version. If tarfile contains source files at
  top level, this parameter must be empty or "."

  Example:

  ```
  abclient-0.2.3-py3-none-any.whl|tar|https://files.pythonhosted.org/packages/49/eb/091b02c1e36d68927adfb746706e2c80f7e7bfb3f16e3cbcfec2632118ab/abclient-0.2.3.tar.gz|abclient-0.2.3i
  ```

* zip: `wheelname|zip|url|basedir|fix_setup`

  Same as `tar`, but in .zip format

  Example:

  ```
  networkx-2.2-py2.py3-none-any.whl|zip|https://files.pythonhosted.org/packages/f3/f4/7e20ef40b118478191cec0b58c3192f822cace858c19505c7670961b76b2/networkx-2.2.zip|networkx-2.2
  ```

* pypi: `wheelname|pypi|url`

  URL of a pre-built wheel for direct download. This type of download avoids
  the build step and reduces overall build times.

  Example:

  ```
  bottle-0.12.18-py3-none-any.whl|pypi|https://files.pythonhosted.org/packages/e9/39/2bf3a1fd963e749cdbe5036a184eda8c37d8af25d1297d94b8b7aeec17c4/bottle-0.12.18-py3-none-any.whl
  ```

