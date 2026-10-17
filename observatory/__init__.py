"""赛博夺权战 · 观测台（M4）。

纯观战：跟着内核写出的 `runs/<id>/` 自动刷新，展示当前 owner、比分、
事件流与各 AI 的回合转写；不负责开局与终局，对局由终端里的命令驱动。

- `observatory.data`：账本 / 回合卷宗的只读解析（不依赖 Qt）；
- `observatory.app`：PySide6 界面（用法：`uv run python -m observatory.app`）。
"""
