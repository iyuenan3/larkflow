"""装出来的包里必须有模板 yaml。

2026-07-27 真机上撞到：`pip install larkflow` 之后 `templates/` 目录里只有一个
`__init__.py`，`load_template("contract")` 抛「模板文件不存在」，**整个引擎一条流程都跑
不起来**。根因是 setuptools 默认只收 `.py`，非代码文件要显式声明 package-data。

从源码树跑永远发现不了（文件就在那儿），只有真装一次才看得见。而「新增业务场景 = 只加
一个 yaml、零 Python」是这个产品对外承诺的核心，它在安装态下完全依赖这条声明。

这里不去真建 wheel（要引入构建依赖、还慢）。钉的是**声明与现实对得上**：模板目录里出现
了新的数据文件后缀而 pyproject 没跟着改，这条就红。
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
import subprocess

from larkflow import __version__

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "larkflow" / "templates"
WORKFLOW_MIGRATIONS = ROOT / "larkflow" / "workflow" / "migrations"
EDGE_DISTRIBUTION_SCRIPTS = (
    ROOT / "deploy" / "larkflow-edge-manager.py",
    ROOT / "deploy" / "build-larkflow-edge-bundle.py",
)


def test_runtime_version_matches_pyproject():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"$', text, re.M)
    assert match, "pyproject.toml 里没有项目 version"
    assert match.group(1) == __version__


def test_edge_distribution_scripts_are_executable_and_parse_with_system_python():
    system_python = Path("/usr/bin/python3")
    for script in EDGE_DISTRIBUTION_SCRIPTS:
        assert os.access(script, os.X_OK), script
        if system_python.exists():
            completed = subprocess.run(
                [str(system_python), str(script), "--help"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
            assert completed.returncode == 0, completed.stderr.decode(
                "utf-8", errors="replace"
            )


def declared_patterns(package: str = "larkflow.templates") -> list[str]:
    """从 pyproject 里抠出指定 package-data 的 glob。

    手写小解析而不是 tomllib：requires-python 已经放到 3.10（Ubuntu 22.04 自带的那个），
    而 tomllib 是 3.11 才有的。为一条测试把运行时下限顶回去，本末倒置。
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"^\[tool\.setuptools\.package-data\]\s*$(.*?)(?=^\[|\Z)",
                      text, re.M | re.S)
    assert block, "pyproject 里没有 [tool.setuptools.package-data]，模板不会被打进包"
    line = re.search(
        rf'^"?{re.escape(package)}"?\s*=\s*\[(.*?)\]',
        block.group(1),
        re.M | re.S,
    )
    assert line, f"package-data 里没有 {package} 这一项"
    return re.findall(r'"([^"]+)"', line.group(1))


def test_every_template_file_is_covered_by_a_package_data_pattern():
    pats = declared_patterns()
    data_files = [p for p in TEMPLATES.iterdir() if p.is_file() and p.suffix != ".py"]
    assert data_files, "templates 目录里一个数据文件都没有？那模板去哪了"
    for f in data_files:
        assert any(fnmatch.fnmatch(f.name, pat) for pat in pats), (
            f"{f.name} 不被 package-data 的 {pats} 覆盖：装出来的包里不会有它，"
            f"load_template 会报「模板文件不存在」")


def test_every_workflow_migration_is_covered_by_package_data():
    pats = declared_patterns("larkflow.workflow.migrations")
    migrations = [
        path
        for path in WORKFLOW_MIGRATIONS.iterdir()
        if path.is_file() and path.suffix != ".py"
    ]
    assert migrations, "workflow migrations 目录里没有 SQL 文件"
    for migration in migrations:
        assert any(fnmatch.fnmatch(migration.name, pat) for pat in pats), (
            f"{migration.name} 不被 package-data 的 {pats} 覆盖，"
            "安装后无法初始化 PostgreSQL"
        )


def test_the_templates_shipped_are_the_ones_the_docs_promise():
    """CLAUDE.md / README 说的是 contract / defect / hiring 三张图。少一张就是承诺没兑现。"""
    names = {p.stem for p in TEMPLATES.glob("*.yaml")}
    assert {"contract", "defect", "hiring"} <= names, names
