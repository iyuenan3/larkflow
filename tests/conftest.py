import os
import sys
from pathlib import Path

import pytest

# 让 `import larkflow` 在任意 cwd 下可用
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _no_env_leak():
    """每条测试跑完把 `os.environ` 复原。

    起因是一次真实的连坐：`main()` 会先 `_preload_env` 把**当前目录的 `.env`** 灌进
    `os.environ`（那是设计如此，`--db` 之类的默认值在建 parser 时就要读到它）。于是任何
    调 `main()` 的 CLI 测试都会把开发机上那份真 `.env` 读进本进程，而 `load_dotenv` 是
    直接写 `os.environ` 的，**`monkeypatch` 跟踪不到，也就不会复原**。

    后果不是抽象的：新增 `tests/test_doctor.py` 之后（字母序排在 `test_llm_proxy` 前面），
    它的 CLI 测试把真 `.env` 里的 `LLM_NO_PROXY` 带了进来，`test_llm_proxy` 那条「默认
    仍然尊重环境变量」当场变红，而它自己一个字没改。

    更难受的是这种红是**跟着机器走**的：绿不绿取决于一份 `.gitignore` 掉的本地文件里有
    什么，换台机器结论就变。测试的价值全在于它是一把稳定的尺子，所以这条在这里收口。
    """
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)
