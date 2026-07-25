"""引擎自己读 `.env`，别让人用 `source` 加载。

起因（2026-07-26 真实踩坑）：我让 Maxwell 用 `set -a; source .env; set +a`，结果
`LARKFLOW_ROLES={"法务":"ou_…"}` 被 **shell 的引号剥离**吃成 `{法务:ou_…}`，
JSON 解析当场炸。`.env` 长得像 shell 赋值，但它不是 shell 脚本：`source` 会做引号剥离、
词分割、glob 展开、`$` 展开、反引号执行；任何含 `"` / 空格 / `#` / `$` 的值都是雷。

所以解析放进引擎，规则明确、与 shell 无关。
"""
from __future__ import annotations

from larkflow.config import load_dotenv


def write(tmp_path, text: str) -> str:
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_quotes_are_part_of_the_syntax_not_part_of_the_value(tmp_path):
    """这就是踩到的那条：JSON 值里的双引号必须原样留下。"""
    env = {}
    load_dotenv(write(tmp_path, 'A={"法务":"ou_x"}\nB="双引号包起来"\nC=\'单引号包起来\'\n'),
                environ=env)
    assert env["A"] == '{"法务":"ou_x"}', "没包引号的值原样保留，一个字符都不许动"
    assert env["B"] == "双引号包起来", "整体包裹的引号是语法，要剥掉"
    assert env["C"] == "单引号包起来"


def test_an_already_exported_variable_wins(tmp_path):
    """显式 export 优先于文件：调试时想临时换一个值，不该被文件盖回去。"""
    env = {"A": "来自 shell"}
    load_dotenv(write(tmp_path, "A=来自文件\nB=只在文件里\n"), environ=env)
    assert env["A"] == "来自 shell"
    assert env["B"] == "只在文件里"


def test_comments_blanks_and_export_prefix(tmp_path):
    env = {}
    load_dotenv(write(tmp_path, "# 整行注释\n\n  \nexport A=1\n   B=2\n"), environ=env)
    assert env == {"A": "1", "B": "2"}


def test_an_inline_comment_is_stripped_only_when_the_value_is_unquoted(tmp_path):
    """`#` 在值里是合法字符（密码、颜色、URL fragment 都可能有）。

    只有「空白 + #」才当注释起点，且引号包裹的值一概不动。
    """
    env = {}
    load_dotenv(write(tmp_path, 'A=1 # 说明\nB=abc#def\nC="1 # 不是注释"\n'), environ=env)
    assert env["A"] == "1"
    assert env["B"] == "abc#def", "紧贴的 # 不是注释"
    assert env["C"] == "1 # 不是注释"


def test_dollar_signs_and_backticks_are_literal(tmp_path):
    """`source` 会展开 `$X` 并执行反引号 —— 一把含 `$` 的 key 会被悄悄改写。"""
    env = {"X": "已存在"}
    load_dotenv(write(tmp_path, "A=sk-$X-`whoami`\n"), environ=env)
    assert env["A"] == "sk-$X-`whoami`"


def test_a_missing_file_is_a_no_op(tmp_path):
    env = {"A": "1"}
    assert load_dotenv(str(tmp_path / "没有这个文件"), environ=env) == []
    assert env == {"A": "1"}


def test_it_reports_which_keys_it_set(tmp_path):
    """启动时打一行「从 .env 读了哪几个键」，人一眼看得出配置到底生效没有。
    **只报键名不报值**：这里面全是凭证。"""
    env = {"B": "已存在"}
    got = load_dotenv(write(tmp_path, "A=1\nB=2\nC=3\n"), environ=env)
    assert got == ["A", "C"], "已被 shell 占用的键不算「本次设置」"


def test_lines_without_an_equals_sign_are_ignored_not_fatal(tmp_path):
    env = {}
    load_dotenv(write(tmp_path, "这行是垃圾\nA=1\n"), environ=env)
    assert env == {"A": "1"}


def test_the_cli_loads_dotenv_before_reading_defaults(tmp_path, monkeypatch):
    """`--db` 之类的默认值在 build_parser 里就从 env 取，所以加载必须更早。"""
    from larkflow.__main__ import main

    monkeypatch.delenv("LARKFLOW_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("LARKFLOW_DB=/tmp/from-dotenv.sqlite\n", encoding="utf-8")
    seen = {}

    def factory(ns):
        seen["db"] = ns.db
        raise SystemExit(0)

    try:
        main(["status", "x"], factory=factory)
    except SystemExit:
        pass
    assert seen["db"] == "/tmp/from-dotenv.sqlite"
