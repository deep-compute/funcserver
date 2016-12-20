from setuptools import setup, find_packages
import os

HERE = os.path.abspath(os.path.dirname(__file__))
def get_long_description():
    dirs = [ HERE ]
    if os.getenv("TRAVIS"):
        dirs.append(os.getenv("TRAVIS_BUILD_DIR"))

    long_description = ""

    for d in dirs:
        rst_readme = os.path.join(d, "README.rst")
        if not os.path.exists(rst_readme):
            continue

        with open(rst_readme) as fp:
            long_description = fp.read()
            return long_description

    return long_description

long_description = get_long_description()

version = '0.2.12'
setup(
    name="funcserver",
    version=version,
    description="Simple and opiniated way to build APIs in Python",
    long_description=long_description,
    keywords='funcserver',
    author='Deep Compute, LLC',
    author_email="contact@deepcompute.com",
    url="https://github.com/deep-compute/funcserver",
    download_url="https://github.com/deep-compute/funcserver/tarball/%s" % version,
    license='MIT License',
    install_requires=[
        'gevent',
        'statsd',
        'requests',
        'tornado',
        'msgpack-python',
        'basescript >= 0.1.6',
    ],
    package_dir={'funcserver': 'funcserver'},
    packages=find_packages('.'),
    include_package_data=True,
    classifiers=[
        "Programming Language :: Python :: 2.7",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
    ]
)
