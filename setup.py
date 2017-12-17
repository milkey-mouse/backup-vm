from pkg_resources import EntryPoint
from setuptools import Command
from setuptools import setup
from itertools import chain
import contextlib
import sys
import io


class build_usage(Command):
    description = "update usage section in README"
    user_options = []

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    def run(self):
        with open("README.rst", "r+") as f:
            lines = [*self.format_readme(f)]
            f.seek(0)
            f.writelines(lines)
            f.truncate()

    def format_readme(self, lines):
        skipping = False
        for line in lines:
            if line == ".. END AUTO-GENERATED USAGE\n":
                skipping = False
            if not skipping:
                yield line
            if line == ".. BEGIN AUTO-GENERATED USAGE\n":
                yield from self.generate_usage()
                skipping = True


    def generate_usage(self):
        old_argv = sys.argv
        scripts = self.distribution.entry_points["console_scripts"]
        for pkg in self.distribution.packages:
            for ep in EntryPoint.parse_group(pkg, scripts).values():
                rs = io.StringIO()
                sys.argv = [None, "--help"]
                with contextlib.redirect_stdout(rs), contextlib.suppress(SystemExit):
                    ep.resolve()()
                rs.seek(0)
                yield "::\n\n"
                for line in rs.readlines():
                    yield ("    " if line != "\n" else "") + line
                yield "\n"
        sys.argv = old_argv
        yield from []


def readme():
    with open("README.rst") as f:
        return f.read()


setup(name="backup-vm",
      use_scm_version={
          "write_to": "backup_vm/_version.py",
      },
      description="Backup libvirt VMs with borg",
      long_description=readme(),
      classifiers=[
          "Development Status :: 4 - Beta",
          "Environment :: Console",
          "Intended Audience :: System Administrators",
          "License :: OSI Approved :: MIT License",
          "Operating System :: POSIX :: Linux",
          "Programming Language :: Python :: 3 :: Only",
          "Programming Language :: Python :: 3.4",
          "Programming Language :: Python :: 3.5",
          "Programming Language :: Python :: 3.6",
          "Topic :: System :: Archiving :: Backup",
      ],
      keywords="borg backup libvirt vm snapshot",
      url="https://github.com/milkey-mouse/backup-vm",
      author="Milkey Mouse",
      author_email="milkeymouse@meme.institute",
      license="MIT",
      packages=["backup_vm"],
      setup_requires=["setuptools_scm>=1.7"],
      install_requires=[
          "libvirt-python",
      ],
      entry_points={
          "console_scripts": [
              "backup-vm=backup_vm.backup:main",
              "restore-vm=backup_vm.restore:main",
              "borg-multi=backup_vm.multi:main",
          ],
      },
      cmdclass={"build_usage": build_usage},
      include_package_data=True,
      zip_safe=False)
