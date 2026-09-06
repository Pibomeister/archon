"""Explicit source shapes and frozen verification tools; no model-selected setup."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

VERSION = "archon.project-profile.v2"
RECIPES = ("archon-engine-bun.v1", "goodword-workflows-python.v1")


def regular(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("Source recipe requires regular metadata files")
    return path


def check_metadata(profile, root):
    recipe = profile["sourceRecipe"]
    if recipe == "archon-engine-bun.v1":
        package = json.loads(regular(root / "package.json").read_text())
        if package.get("name") != "archon" or package.get("version") != "0.10.1" or "packages/*" not in package.get("workspaces", []):
            raise ValueError("Engine recipe source metadata drift")
        regular(root / "bun.lock")
    elif recipe == "goodword-workflows-python.v1":
        if (root / "package.json").exists():
            raise ValueError("Workflow-pack recipe does not assume a Node repository")
        for name in ("setup/package.sh", "setup/repo-policy.py", "setup/parse-review-envelope.py"):
            regular(root / name)
        if not (root / "workflows").is_dir() or (root / "workflows").is_symlink():
            raise ValueError("Workflow-pack recipe requires its workflows directory")
    else:
        raise ValueError("Unknown source recipe")


def binary_digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def tool(name, arguments, expected):
    located = shutil.which(name)
    if not located:
        raise ValueError("Missing source-recipe tool: " + name)
    path = Path(located).resolve()
    result = subprocess.run([str(path), *arguments], check=True, capture_output=True, text=True, timeout=60)
    version = result.stdout.strip().splitlines()[0]
    if not (version == expected or version.startswith(expected + " ")):
        raise ValueError("Source-recipe tool version mismatch: " + name)
    return {"path": str(path), "version": version, "sha256": binary_digest(path)}


def facts(profile, root):
    check_metadata(profile, root)
    result = {"git": tool("git", ["--version"], "git version")}
    if profile["sourceRecipe"] == "archon-engine-bun.v1":
        result["bun"] = tool("bun", ["--version"], "1.3.14")
    else:
        result["uv"] = tool("uv", ["--version"], "uv 0.12.10")
        result["bash"] = tool("bash", ["--version"], "GNU bash,")
        # Dependencies must already be prepared. Offline resolution prevents a
        # verification phase from adding network-selected dependencies.
        command = [result["uv"]["path"], "run", "--offline", "--no-project", "--python", "3.13.9", "--with", "pyyaml==6.0.3", "python", "-c", "import json,platform,sys,yaml; print(json.dumps({'version':platform.python_version(),'path':sys.executable,'pyyaml':yaml.__version__}))"]
        process = subprocess.run(command, cwd=root, check=True, capture_output=True, text=True, timeout=120)
        python = json.loads(process.stdout)
        if python["version"] != "3.13.9" or python["pyyaml"] != "6.0.3":
            raise ValueError("Workflow-pack Python dependency version drift")
        executable = Path(python["path"]).resolve()
        result["python"] = {"path": str(executable), "version": python["version"], "sha256": binary_digest(executable)}
        result["pyyaml"] = python["pyyaml"]
    return result


def verification_argv(profile, captured_tools, argv):
    if profile.get("profileVersion") != VERSION:
        return argv
    allowed = ("bun",) if profile["sourceRecipe"] == "archon-engine-bun.v1" else ("uv", "bash")
    if argv[0] not in allowed:
        raise ValueError("Verification command is outside the source recipe")
    return [captured_tools[argv[0]]["path"], *argv[1:]]
