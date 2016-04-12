from setuptools import setup, find_packages

setup(
    name="funcserver",
    version='0.1',
    description="Simple and opiniated way to build APIs in Python",
    keywords='funcserver',
    author='Prashanth Ellina',
    author_email="Use the github issues",
    url="https://github.com/deep-compute/funcserver",
    license='MIT License',
    install_requires=[
        'gevent',
        'statsd',
        'requests',
        'tornado',
        'msgpack-python',
        'basescript',
    ],
    dependency_links=[
        'http://github.com/deep-compute/basescript/tarball/master#egg=basescript'
    ],
    package_dir={'funcserver': 'funcserver'},
    packages=find_packages('.'),
    include_package_data=True
)
