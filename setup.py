from setuptools import find_packages, setup

setup(
    name="polar-station-foundation",
    version="0.1.0",
    description="极地科考站协作基础服务",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.11",
)
