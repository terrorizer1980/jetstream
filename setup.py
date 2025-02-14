from setuptools import setup


def text_from_file(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


test_dependencies = [
    "coverage",
    "jsonschema",
    "pytest",
    "pytest-black",
    "pytest-cov",
    "pytest-flake8",
    "mypy",
]

extras = {
    "testing": test_dependencies,
}

setup(
    name="mozilla-jetstream",
    author="Mozilla Corporation",
    author_email="fx-data-dev@mozilla.org",
    description="Runs a thing that analyzes experiments",
    url="https://github.com/mozilla/jetstream",
    packages=[
        "jetstream",
        "jetstream.config",
        "jetstream.tests",
        "jetstream.tests.integration",
        "jetstream.logging",
        "jetstream.workflows",
    ],
    package_data={
        "jetstream.config": ["*.toml"],
        "jetstream.tests": ["data/*"],
        "jetstream.workflows": ["*.yaml"],
    },
    install_requires=[
        "attrs",
        "cattrs",
        "Click",
        "dask[distributed]",
        "GitPython",
        "google-cloud-bigquery",
        "google-cloud-bigquery-storage",
        "google-cloud-container",
        "google-cloud-storage",
        "grpcio",  # https://github.com/googleapis/google-cloud-python/issues/6259
        "jinja2",
        "mozanalysis",
        "pyarrow",
        "pytz",
        "PyYAML",
        "requests",
        "smart_open[gcp]",
        "statsmodels",
        "toml",
    ],
    tests_require=test_dependencies,
    extras_require=extras,
    long_description=text_from_file("README.md"),
    long_description_content_type="text/markdown",
    python_requires=">=3.6",
    entry_points="""
        [console_scripts]
        pensieve=jetstream.cli:cli
        jetstream=jetstream.cli:cli
    """,
    # This project does not issue releases, so this number is not meaningful
    # and should not need to change.
    version="2021.1.0",
)
