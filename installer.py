#!/usr/bin/env python3
"""Cross-platform conda installer for the radiomics pipeline."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_ENV_NAME = os.environ.get("ENV_NAME", "able")
PYTHON_VERSION = os.environ.get("PYTHON_VERSION", "3.9")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create a conda environment and install the radiomics pipeline "
            "dependencies on Linux, macOS, or Windows."
        )
    )
    parser.add_argument(
        "-n",
        "--env-name",
        default=DEFAULT_ENV_NAME,
        help=f"conda environment name to create/use (default: {DEFAULT_ENV_NAME})",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="remove the conda environment first if it already exists",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the commands without running them",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent
    requirements = repo_root / "requirements.txt"
    pyfe_requirements = repo_root / "requirements-pyfe.txt"
    for required_file in (requirements, pyfe_requirements):
        if not required_file.is_file():
            print(f"ERROR: missing required file: {required_file}", file=sys.stderr)
            return 1

    conda = find_conda()
    if conda is None:
        print_conda_missing_message()
        return 1

    print(f"Using conda: {conda}")

    if args.recreate:
        if args.dry_run or env_exists(conda, args.env_name, args.dry_run):
            run([conda, "env", "remove", "-n", args.env_name, "-y"], args.dry_run)

    if not env_exists(conda, args.env_name, args.dry_run):
        run(
            [
                conda,
                "create",
                "-n",
                args.env_name,
                f"python={PYTHON_VERSION}",
                "pip",
                "git",
                "-y",
            ],
            args.dry_run,
        )
    else:
        print(f"Conda environment '{args.env_name}' already exists; using it.")

    ensure_python_version(conda, args.env_name, args.dry_run)

    pip_install(conda, args.env_name, args.dry_run, "--upgrade", "pip", "setuptools", "wheel")
    pip_install(
        conda,
        args.env_name,
        args.dry_run,
        "numpy>=1.23,<2.0",
        "Cython<3",
    )
    pip_install(
        conda,
        args.env_name,
        args.dry_run,
        "--no-build-isolation",
        "PyRadiomics==3.0.1",
    )
    pip_install(conda, args.env_name, args.dry_run, "-r", str(requirements))
    pip_install(
        conda,
        args.env_name,
        args.dry_run,
        "--ignore-requires-python",
        "--no-deps",
        "-r",
        str(pyfe_requirements),
    )

    run(
        conda_python(
            conda,
            args.env_name,
            "-c",
            "import SimpleITK, radiomics, pyfe, pyable; print('All imports successful')",
        ),
        args.dry_run,
    )

    print()
    print(f"Environment '{args.env_name}' is ready.")
    print(f"Activate it with: conda activate {args.env_name}")
    return 0


def find_conda() -> str | None:
    conda = shutil.which("conda")
    if conda:
        return conda

    candidates = []
    system = platform.system().lower()
    home = Path.home()

    if system == "windows":
        user_profile = Path(os.environ.get("USERPROFILE", str(home)))
        candidates.extend(
            [
                user_profile / "miniconda3" / "Scripts" / "conda.exe",
                user_profile / "anaconda3" / "Scripts" / "conda.exe",
                Path("C:/ProgramData/miniconda3/Scripts/conda.exe"),
                Path("C:/ProgramData/anaconda3/Scripts/conda.exe"),
            ]
        )
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.extend(
                [
                    Path(local_app_data) / "miniconda3" / "Scripts" / "conda.exe",
                    Path(local_app_data) / "anaconda3" / "Scripts" / "conda.exe",
                ]
            )
    else:
        candidates.extend(
            [
                home / "miniconda3" / "bin" / "conda",
                home / "anaconda3" / "bin" / "conda",
                Path("/opt/miniconda3/bin/conda"),
                Path("/opt/anaconda3/bin/conda"),
            ]
        )

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    return None


def print_conda_missing_message() -> None:
    system = platform.system().lower()
    print("ERROR: conda was not found.", file=sys.stderr)
    print("Install Miniconda or Anaconda, then rerun this installer.", file=sys.stderr)

    if system == "windows":
        print(
            "Windows: download Miniconda from "
            "https://docs.conda.io/en/latest/miniconda.html and enable "
            "'Add Miniconda3 to PATH' or run this from the Anaconda Prompt.",
            file=sys.stderr,
        )
    elif system == "darwin":
        machine = platform.machine().lower()
        arch = "arm64" if machine in {"arm64", "aarch64"} else "x86_64"
        print(
            "macOS: install Miniconda for "
            f"{arch} from https://docs.conda.io/en/latest/miniconda.html, "
            "then open a new terminal.",
            file=sys.stderr,
        )
    else:
        print(
            "Linux: install Miniconda from "
            "https://docs.conda.io/en/latest/miniconda.html, then open a new shell.",
            file=sys.stderr,
        )


def env_exists(conda: str, env_name: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"+ {format_command([conda, 'env', 'list', '--json'])}")
        return False

    result = subprocess.run(
        [conda, "env", "list", "--json"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        raise SystemExit(result.returncode)

    envs = json.loads(result.stdout).get("envs", [])
    normalized_env_name = os.path.normcase(env_name)
    for env_path in envs:
        if os.path.normcase(Path(env_path).name) == normalized_env_name:
            return True
    return False


def ensure_python_version(conda: str, env_name: str, dry_run: bool) -> None:
    command = [
        conda,
        "run",
        "-n",
        env_name,
        "python",
        "-c",
        "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
    ]
    if dry_run:
        print(f"+ {format_command(command)}")
        return

    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        raise SystemExit(result.returncode)

    python_version = result.stdout.strip().splitlines()[-1]
    if python_version != PYTHON_VERSION:
        print(
            f"ERROR: conda environment '{env_name}' uses Python {python_version}, "
            f"but this project requires Python {PYTHON_VERSION}.",
            file=sys.stderr,
        )
        print(f"Rerun with --recreate to rebuild '{env_name}'.", file=sys.stderr)
        raise SystemExit(1)


def pip_install(conda: str, env_name: str, dry_run: bool, *args: str) -> None:
    run(conda_python(conda, env_name, "-m", "pip", "install", *args), dry_run)


def conda_python(conda: str, env_name: str, *args: str) -> list[str]:
    return [conda, "run", "--no-capture-output", "-n", env_name, "python", *args]


def run(command: list[str], dry_run: bool) -> None:
    print(f"+ {format_command(command)}")
    if dry_run:
        return

    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc


def format_command(command: list[str]) -> str:
    return subprocess.list2cmdline(command) if platform.system() == "Windows" else shlex_join(command)


def shlex_join(command: list[str]) -> str:
    import shlex

    return shlex.join(command)


if __name__ == "__main__":
    raise SystemExit(main())
