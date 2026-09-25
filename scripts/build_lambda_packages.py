"""Builds the Lambda deployment packages: one zip per function, with only
that function's packages and their dependencies, as Linux wheels for the
Lambda runtime (whatever OS this runs on).

    python scripts/build_lambda_packages.py              # all functions
    python scripts/build_lambda_packages.py thumbnailer  # just one

Writes build/lambda/<function>.zip, the paths Terraform
(infra/terraform/live) deploys by default. Run it with Python 3.14 (the
runtime's version); needs network access to PyPI.
"""

import argparse
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
OUT_DIR = ROOT / "build" / "lambda"
# Scratch space, at a fixed path: pip records where each local wheel was
# installed from (direct_url.json), so a random temporary directory would
# make every build's zip different and redeploy every function.
WORK_DIR = ROOT / "build" / "lambda-work"

PYTHON_VERSION = "3.14"
# Lambda's Python 3.14 runtime runs on Amazon Linux 2023 (glibc 2.34), so
# any manylinux wheel up to 2.34 works. pip doesn't infer the older tags
# from the newest one, so each is listed.
_MANYLINUX_TAGS = ["manylinux_2_34", "manylinux_2_28", "manylinux_2_17", "manylinux2014"]
PLATFORMS = {arch: [f"{tag}_{machine}" for tag in _MANYLINUX_TAGS] for arch, machine in [("arm64", "aarch64"), ("x86_64", "x86_64")]}

# Function -> local packages, in install order. `stash-shared[aws]` brings
# Powertools, which logging and metrics use with PLATFORM=aws.
FUNCTIONS = {
    "api": ["shared", "api"],
    "thumbnailer": ["shared", "workers/core", "workers/thumbnailer"],
    "image_analyzer": ["shared", "workers/core", "workers/image_analyzer"],
    "document_analyzer": ["shared", "workers/core", "workers/document_analyzer"],
    "embedding_worker": ["shared", "workers/core", "workers/embedding_worker"],
    # `alembic upgrade head` in the VPC (app.aws_lambda_migrations), run by
    # the deployment: the API's code plus its migrations.
    "migrations": ["shared", "api"],
}
EXTRAS = {"shared": "[aws]"}
# Function -> files copied into the package (source relative to backend/
# -> path in the zip), for what isn't part of any wheel. Not at the root:
# the scripts' `alembic/` would collide with the alembic library there.
EXTRA_FILES = {
    "migrations": {"api/alembic.ini": "migrations/alembic.ini", "api/alembic": "migrations/alembic"},
}

# Never needed at runtime; skipped when zipping.
_SKIP_DIRS = {"__pycache__", "tests"}
_SKIP_SUFFIXES = {".pyc", ".pyi"}


def build(function: str, platforms: list[str]) -> Path:
    tmp = WORK_DIR / function
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        target = tmp / "package"
        # The local packages are pure Python: build each one's wheel here,
        # then resolve everything together for the target platform.
        requirements = []
        for i, package in enumerate(FUNCTIONS[function]):
            wheels = tmp / "wheels" / str(i)
            _pip("wheel", "--no-deps", "--wheel-dir", wheels, BACKEND / package)
            [wheel] = wheels.glob("*.whl")
            requirements.append(f"{wheel.as_posix()}{EXTRAS.get(package, '')}")
        _pip(
            "install",
            "--target", target,
            *(arg for platform in platforms for arg in ("--platform", platform)),
            "--implementation", "cp",
            "--python-version", PYTHON_VERSION,
            "--only-binary=:all:",
            "--upgrade",
            # pip would compare against the environment it runs in, not the target.
            "--no-warn-conflicts",
            *requirements,
        )
        for source, destination in EXTRA_FILES.get(function, {}).items():
            (target / destination).parent.mkdir(parents=True, exist_ok=True)
            if (BACKEND / source).is_dir():
                shutil.copytree(BACKEND / source, target / destination)
            else:
                shutil.copy2(BACKEND / source, target / destination)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        archive = OUT_DIR / f"{function}.zip"
        _zip(target, archive)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return archive


def _zip(source: Path, archive: Path) -> None:
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if path.is_dir() or _SKIP_DIRS & set(relative.parts) or path.suffix in _SKIP_SUFFIXES:
                continue
            # Fixed timestamps and permissions: the same inputs give the
            # same zip, so Terraform only redeploys what actually changed.
            info = zipfile.ZipInfo(relative.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())


def _pip(*args) -> None:
    subprocess.run([sys.executable, "-m", "pip", "--disable-pip-version-check", "--quiet", *map(str, args)], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("functions", nargs="*", metavar="function", help=f"default: all ({', '.join(FUNCTIONS)})")
    parser.add_argument("--architecture", choices=PLATFORMS, default="arm64")
    args = parser.parse_args()
    if unknown := set(args.functions) - FUNCTIONS.keys():
        parser.error(f"unknown function(s): {', '.join(sorted(unknown))}")
    for function in args.functions or FUNCTIONS:
        archive = build(function, PLATFORMS[args.architecture])
        sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
        print(f"{archive.relative_to(ROOT)}: {archive.stat().st_size / 1024 / 1024:.1f} MiB, sha256 {sha256}")


if __name__ == "__main__":
    main()
