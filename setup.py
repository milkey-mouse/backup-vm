from setuptools import setup


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
              "borg-multi=backup_vm.multi:main",
          ],
      },
      include_package_data=True,
      zip_safe=False)
