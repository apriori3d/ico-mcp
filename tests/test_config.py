from pathlib import Path

import pytest

from ico_mcp.config import (
    Config,
    ConfigError,
    GithubConfig,
    Timeouts,
    load_config,
)


def make_repo(root: Path, toml: str, package: str = "domain") -> Path:
    for directory in (package, f"{package}_draft"):
        (root / directory).mkdir()
        (root / directory / "__init__.py").write_text("")
    (root / "ico.toml").write_text(toml)
    return root


MINIMAL = 'name = "demo"\npackage = "domain"\n'


def test_minimal_config_uses_defaults(tmp_path: Path) -> None:
    config = load_config(make_repo(tmp_path, MINIMAL))

    assert config == Config(root=tmp_path.resolve(), name="demo", package="domain")
    assert config.github is None
    assert config.timeouts == Timeouts(run=600, validate=30, load=60, wait=45)
    assert config.env == ()
    assert config.typecheck == "strict"


def test_full_config(tmp_path: Path) -> None:
    toml = """
name = "vision"
package = "vision"
github = { repo = "owner/vision-mcp", base = "dev" }
timeouts = { run = 120, validate = 5.5, load = 10, wait = 20 }
env = ["HF_HOME", "CUDA_VISIBLE_DEVICES"]
typecheck = "standard"
"""
    config = load_config(make_repo(tmp_path, toml, package="vision"))

    assert config.github == GithubConfig(repo="owner/vision-mcp", base="dev")
    assert config.timeouts == Timeouts(run=120, validate=5.5, load=10, wait=20)
    assert config.env == ("HF_HOME", "CUDA_VISIBLE_DEVICES")
    assert config.typecheck == "standard"


def test_partial_timeouts_and_default_base(tmp_path: Path) -> None:
    toml = MINIMAL + 'timeouts = { run = 60 }\ngithub = { repo = "o/r" }\n'
    config = load_config(make_repo(tmp_path, toml))

    assert config.timeouts == Timeouts(run=60)
    assert config.github == GithubConfig(repo="o/r", base="main")


def test_layout_paths(tmp_path: Path) -> None:
    config = load_config(make_repo(tmp_path, MINIMAL))
    root = tmp_path.resolve()

    assert config.stable_dir == root / "domain"
    assert config.draft_dir == root / "domain_draft"
    assert config.pipelines_dir == root / "pipelines"
    assert config.examples_dir == root / "examples"
    assert config.runs_dir == root / "runs"


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="ico.toml not found"):
        load_config(tmp_path)


def test_invalid_toml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(make_repo(tmp_path, "name = "))


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ('package = "domain"\n', "name: required"),
        ('name = "demo"\n', "package: required"),
        ('name = ""\npackage = "domain"\n', "name: expected a non-empty string"),
        ('name = 1\npackage = "domain"\n', "name: expected a non-empty string"),
        (
            'name = "demo"\npackage = "my-domain"\n',
            "package: expected a Python identifier",
        ),
        (
            MINIMAL + "timeouts = { run = 0 }\n",
            "timeouts.run: expected a positive number",
        ),
        (
            MINIMAL + 'timeouts = { run = "x" }\n',
            "timeouts.run: expected a positive number, got 'x'",
        ),
        (
            MINIMAL + "timeouts = { load = true }\n",
            "timeouts.load: expected a positive number",
        ),
        (
            MINIMAL + "timeouts = { wait = -1 }\n",
            "timeouts.wait: expected a positive number",
        ),
        (MINIMAL + "timeouts = { kill = 5 }\n", "unknown keys: timeouts.kill"),
        (MINIMAL + "timeouts = 5\n", "timeouts: expected a table"),
        (MINIMAL + 'env = "HF_HOME"\n', "env: expected a list"),
        (
            MINIMAL + 'env = ["HF-HOME"]\n',
            r"env\[0\]: expected an environment variable name",
        ),
        (
            MINIMAL + 'github = { repo = "owner" }\n',
            "github.repo: expected 'owner/name'",
        ),
        (MINIMAL + 'github = { base = "main" }\n', "github.repo: required"),
        (
            MINIMAL + 'github = { repo = "o/r", branch = "x" }\n',
            "unknown keys: github.branch",
        ),
        (MINIMAL + 'typecheck = "off"\n', "typecheck: expected one of"),
        (MINIMAL + 'pakage = "x"\n', "unknown keys: pakage"),
    ],
)
def test_invalid_fields(tmp_path: Path, toml: str, message: str) -> None:
    make_repo(tmp_path, "")
    (tmp_path / "ico.toml").write_text(toml)

    with pytest.raises(ConfigError, match=message):
        load_config(tmp_path)


def test_package_directory_must_exist(tmp_path: Path) -> None:
    make_repo(tmp_path, 'name = "demo"\npackage = "vision"\n')

    with pytest.raises(ConfigError, match="vision is not a package"):
        load_config(tmp_path)


def test_draft_package_must_exist(tmp_path: Path) -> None:
    make_repo(tmp_path, MINIMAL)
    (tmp_path / "domain_draft" / "__init__.py").unlink()

    with pytest.raises(ConfigError, match="domain_draft is not a package"):
        load_config(tmp_path)
