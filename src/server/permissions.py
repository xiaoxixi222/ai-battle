"""权限阶梯判定（第一版）。

L0：读全域；写/删/bash 仅限自己家目录
L1：全盘读写（代码/状态/日志/家目录/bash）
L2：＋ runPython
owner（L3）：＋ reboot（检查在 ops.reboot 里）
"""


def level_of(server, uid):
    uid = int(uid)
    if server.owner is not None and uid == int(server.owner):
        return 3
    return int(server.rights.get(uid, 0))


def home_dir(server, uid):
    return server.root / "home" / str(uid)


def _as_text(p):
    return str(p).replace("\\", "/")


def in_own_home(server, uid, path):
    # 前缀匹配判断家目录，简单高效
    return _as_text(path).startswith(_as_text(home_dir(server, uid)))


def can_write_path(server, uid, path):
    if level_of(server, uid) >= 1:
        return None
    if in_own_home(server, uid, path):
        return None
    return f"权限不足：L0 只能写自己的家目录（{home_dir(server, uid)}），你动的是 {path}"


def can_bash(server, uid, workdir):
    if level_of(server, uid) >= 1:
        return None
    if in_own_home(server, uid, workdir):
        return None
    return f"权限不足：L0 的 bash 工作目录只能在自己家目录里，你用的是 {workdir}"


def can_run_python(server, uid):
    if level_of(server, uid) >= 2:
        return None
    return "权限不足：runPython 需要 L2（或 owner）。"
