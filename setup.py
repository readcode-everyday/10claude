from setuptools import setup, find_packages

setup(
    name="openclaw",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "anthropic>=0.40.0",
        "openai>=1.50.0",
        "fastapi>=0.115.0",
        "uvicorn[standard]>=0.32.0",
        "pyyaml>=6.0",
        "rich>=13.7.0",
    ],
    entry_points={
        "console_scripts": [
            "openclaw=openclaw.cli:main",
        ],
    },
    python_requires=">=3.11",
)
