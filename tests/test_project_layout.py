from pathlib import Path
import tomllib

import coding_agent


ROOT = Path(__file__).resolve().parents[1]


def test_package_is_imported_from_src_layout() -> None:
    package_path = Path(coding_agent.__file__).resolve()

    assert package_path.is_relative_to(ROOT / "src" / "coding_agent")
    assert not (ROOT / "coding_agent").exists()


def test_pyproject_uses_src_layout() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert config["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]
    assert config["tool"]["pytest"]["ini_options"]["pythonpath"] == ["src"]
