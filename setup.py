#!/usr/bin/env python3
"""
Setup script for rhylthyme: the library and the rhylthyme command
"""

import os

from setuptools import find_packages, setup


# Read the README file
def read_readme():
    readme_path = os.path.join(os.path.dirname(__file__), "README.md")
    if os.path.exists(readme_path):
        with open(readme_path, "r", encoding="utf-8") as f:
            return f.read()
    return "Schedule work a person follows against a clock, and run or publish it."


setup(
    name="rhylthyme",
    version="0.5.0",
    description=(
        "Schedule work a person follows against a clock (recipes, lab protocols, "
        "run-of-shows, workouts): validate, plan and run it, and publish it as a "
        "live timeline."
    ),
    long_description=read_readme(),
    long_description_content_type="text/markdown",
    author="Jeremy Leipzig",
    author_email="leipzig@gmail.com",
    license="Apache-2.0",
    url="https://github.com/rhylthyme/rhylthyme",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    package_data={
        # Prompt copies used by `eval-prompts --patterns baseline`.
        "rhylthyme.cli.eval.patterns": ["*.md"],
    },
    # The base install is the library plus what the rhylthyme command itself
    # needs (click, colorama); those are imported only inside rhylthyme.cli.
    install_requires=[
        "setuptools",
        "click>=8.0.0",
        "jsonschema>=4.0.0",
        "pyyaml>=6.0",
        "colorama>=0.4.0",
        "rhylthyme-spec>=0.2.2a0",
    ],
    extras_require={
        # `rhylthyme import`: recipes and protocols from external sources
        "importers": ["rhylthyme-importers>=0.4.6"],
        # `rhylthyme render`: SVG, PNG and PDF figures (needs Node.js)
        "timeline": ["rhylthyme-timeline>=2.0.0b5"],
        # Everything `pip install rhylthyme` installed up to 0.2.9
        "all": ["rhylthyme-importers>=0.4.6", "rhylthyme-timeline>=2.0.0b5"],
        # Instrument steps on galago-tools lab instruments (--workcell, rhylthyme bridge)
        "galago": ["rhylthyme-galago>=0.2.0a0"],
        # Instrument steps on LabMCP instrument servers (driver "labmcp" in a workcell)
        "labmcp": ["rhylthyme-labmcp>=0.1.0a0"],
        "dev": [
            # Testing framework
            "pytest>=6.0.0",
            "pytest-cov>=2.10.0",
            "pytest-mock>=3.0.0",
            "pytest-xdist>=2.0.0",  # Parallel test execution
            # Code quality
            "black>=21.0.0",
            "flake8>=3.8.0",
            "isort>=5.0.0",
            # Type checking
            "mypy>=0.800",
            # Testing utilities
            "responses>=0.12.0",  # Mock HTTP responses
            "freezegun>=1.0.0",  # Mock datetime
        ],
        "docs": [
            "sphinx>=4.0.0",
            "sphinx-rtd-theme>=1.0.0",
        ],
        "build": [
            "build>=0.7.0",
            "twine>=3.4.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "rhylthyme=rhylthyme.cli:main",
        ],
    },
    python_requires=">=3.12",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: System :: Distributed Computing",
    ],
    keywords="scheduling, timeline, recipes, lab-protocols, real-time, cli, validation",
    project_urls={
        "Homepage": "https://www.rhylthyme.com",
        "Documentation": "https://docs.rhylthyme.com",
        "Bug Reports": "https://github.com/rhylthyme/rhylthyme/issues",
        "Source": "https://github.com/rhylthyme/rhylthyme",
    },
)
