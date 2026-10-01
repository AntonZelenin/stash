"""Builds the Lambda deployment packages: one zip per function, with only
that function's packages and their dependencies, as Linux wheels for the
Lambda runtime (whatever OS this runs on); and the Lambda layers.

    python scripts/build_lambda_packages.py              # everything
    python scripts/build_lambda_packages.py thumbnailer  # just one
    python scripts/build_lambda_packages.py ffmpeg       # the ffmpeg layer

Writes build/lambda/<function>.zip and build/lambda/<layer>.zip, the paths
Terraform (infra/terraform/live) deploys by default. Run it with Python
3.14 (the runtime's version); needs network access to PyPI.

Layers (`LAYERS`) hold what isn't Python: the ffmpeg binary the video
analyzer runs. It's in its own layer rather than the function's zip, which
it would push past Lambda's 50 MB limit on directly uploaded packages.
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
    "video_analyzer": ["shared", "workers/core", "workers/video_analyzer"],
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

# The ffmpeg layer's binary: the static Linux build bundled in the
# imageio-ffmpeg wheel (ffmpeg 7.0.2), taken from PyPI pinned by version and
# hash, so the same input always gives the same layer. Only the binary goes
# in, at bin/ffmpeg, so on Lambda it's /opt/bin/ffmpeg (FFMPEG_PATH). Keep
# in step with backend/workers/video_analyzer/Dockerfile, which takes the
# local image's ffmpeg from the same wheel.
FFMPEG_WHEEL = "imageio-ffmpeg==0.6.0"
FFMPEG_WHEEL_SHA256 = {
    "arm64": "1d47bebd83d2c5fc770720d211855f208af8a596c82d17730aa51e815cdee6dc",
    "x86_64": "c7e46fcec401dd990405049d2e2f475e2b397779df2519b544b8aab515195282",
}
_FFMPEG_MEMBER_PREFIX = "imageio_ffmpeg/binaries/ffmpeg-linux-"

# Layer -> its builder (architecture -> zip written).
LAYERS = {"ffmpeg": lambda architecture: build_ffmpeg_layer(architecture)}

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


def build_ffmpeg_layer(architecture: str) -> Path:
    """build/lambda/ffmpeg.zip: `bin/ffmpeg` (executable), from the pinned
    wheel for `architecture`, its hash checked."""
    tmp = WORK_DIR / "ffmpeg"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        _pip(
            "download",
            FFMPEG_WHEEL,
            "--no-deps",
            "--only-binary=:all:",
            *(arg for platform in PLATFORMS[architecture] for arg in ("--platform", platform)),
            "--python-version", PYTHON_VERSION,
            "--implementation", "cp",
            "--dest", tmp,
        )
        [wheel] = tmp.glob("*.whl")
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if digest != FFMPEG_WHEEL_SHA256[architecture]:
            raise SystemExit(f"{wheel.name}: sha256 {digest}, expected {FFMPEG_WHEEL_SHA256[architecture]}")
        with zipfile.ZipFile(wheel) as zf:
            [member] = [name for name in zf.namelist() if name.startswith(_FFMPEG_MEMBER_PREFIX)]
            binary = zf.read(member)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        archive = OUT_DIR / "ffmpeg.zip"
        archive.unlink(missing_ok=True)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo("bin/ffmpeg", date_time=(1980, 1, 1, 0, 0, 0))
            # Executable: Lambda keeps a layer's file modes. Made on Unix
            # (3) whatever builds it, or the mode is ignored when it's
            # extracted (zipfile says MS-DOS on Windows).
            info.create_system = 3
            info.external_attr = 0o755 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, binary)
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
    targets = [*FUNCTIONS, *LAYERS]
    parser.add_argument("targets", nargs="*", metavar="target", help=f"default: all ({', '.join(targets)})")
    parser.add_argument("--architecture", choices=PLATFORMS, default="arm64")
    args = parser.parse_args()
    if unknown := set(args.targets) - set(targets):
        parser.error(f"unknown function(s) or layer(s): {', '.join(sorted(unknown))}")
    for target in args.targets or targets:
        if target in LAYERS:
            archive = LAYERS[target](args.architecture)
        else:
            archive = build(target, PLATFORMS[args.architecture])
        sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
        print(f"{archive.relative_to(ROOT)}: {archive.stat().st_size / 1024 / 1024:.1f} MiB, sha256 {sha256}")


if __name__ == "__main__":
    main()
