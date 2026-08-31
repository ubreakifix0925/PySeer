# -*- coding: utf-8 -*-
"""出招模式(技能循环)配置包 —— 把"某只精灵怎么出招"固定成文件, 供各脚本直接调用.

为什么放在 app/petplans/ 而不是 app/scripts/:
    WebUI 的脚本列表 = app/scripts 下的 *.py 文件(见 webui.py::list_scripts),
    放那里会被当成可运行脚本, 容易误点执行. 本包只被 import, **不会**出现在脚本列表里.

------------------------------------------------------------------------------
一、怎么写一个出招模式文件
------------------------------------------------------------------------------
在本目录新建 <你起的名字>.py, 里面只需要一个 PLANS 字典:

    PLANS = {
        4648: [(5, 37381), 37383],        # 先用 5 次 37381, 之后一直用 37383
        3022: 19248,                      # 一直用 19248
        3437: {"rotation": 31116, "note": "备注随便写"},
    }

写法(每个精灵的 rotation):

    37383                          一直用这个技能(裸整数)
    (5, 37381)                     用 5 次这个技能
    [(5, 37381), 37383]            按顺序: 5 次 37381, 然后一直 37383
    {"skill": 37381, "times": 5}   同 (5, 37381), 字典写法更直观, 还能加 "note"

- 列表里的**裸整数 = 一直用**, 所以它只能放在最后; 放中间的话后面的项永远轮不到(加载时会警告).
- 如果最后一步是有限次数, 用完后**重复最后一个技能**(不会没招可出).
- 计数按"每场对战 + 每只精灵"独立: 换宠再换回来会**接着数**, 新的一场对战 reset() 清零.

------------------------------------------------------------------------------
二、脚本里怎么用
------------------------------------------------------------------------------
    import petplans                                  # app 已在 PYTHONPATH 里
    runner = petplans.load_runner("默认")            # 读 app/petplans/默认.py
    print(runner.describe())                         # 打印可读的出招表

    runner.reset()                                   # 每场对战开始时清零
    while not battle.finished:
        pid = battle.my.get("id")                    # 当前出战精灵的物种 id
        sid = runner.next_skill(pid, battle.skills, fallback=battle.skills[0])
        battle.use_skill_smart(sid)
        runner.advance(pid)                          # 出招成功后推进计数

load_runner 也接受路径: petplans.load_runner("/path/to/我的.py").
"""
from __future__ import annotations

import importlib.util
import json
import os

PLAN_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(PLAN_DIR)), "data")

INF = None          # rotation 里 times=None 表示"一直用"


# ------------------------------ 名称查询(仅用于打印) ------------------------------
def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_PET_NAMES = None


def pet_name(pid) -> str:
    """物种 id -> 精灵名(查不到返回空串)."""
    global _PET_NAMES
    if _PET_NAMES is None:
        _PET_NAMES = _load_json(os.path.join(_DATA_DIR, "monster_names.json"))
    return str(_PET_NAMES.get(str(pid), "") or "")


_SKILL_NAMES = {}
_SKILL_LOADED = False


def skill_name(sid) -> str:
    """技能 id -> 技能名. skills.json 很大(~16MB), 只在第一次真正需要时读一次并只留名字."""
    global _SKILL_LOADED
    if not _SKILL_LOADED:
        _SKILL_LOADED = True
        raw = _load_json(os.path.join(_DATA_DIR, "skills.json"))
        for k, v in (raw.items() if isinstance(raw, dict) else []):
            if isinstance(v, dict) and v.get("name"):
                _SKILL_NAMES[str(k)] = str(v["name"])
        del raw
    return _SKILL_NAMES.get(str(sid), "")


# ------------------------------ 解析 rotation ------------------------------
class PlanError(Exception):
    """出招模式文件写错了."""


def _is_pair(x) -> bool:
    """(5, 37381) 这种 (次数, 技能) 二元组."""
    return (isinstance(x, (list, tuple)) and len(x) == 2
            and all(isinstance(v, int) or v is None for v in x)
            and (x[0] is None or (isinstance(x[0], int) and x[0] >= 1)))


def _sid(v, who) -> int:
    if not isinstance(v, int) or v <= 0:
        raise PlanError("%s: 技能 id 必须是正整数, 得到 %r" % (who, v))
    return int(v)


def _times(v, who):
    if v is None:
        return None
    if not isinstance(v, int) or v < 1:
        raise PlanError("%s: 次数必须是 >=1 的整数或 None(一直用), 得到 %r" % (who, v))
    return int(v)


def _one_step(it, who) -> tuple:
    if isinstance(it, int):
        return (None, _sid(it, who))
    if isinstance(it, dict):
        sid = it.get("skill", it.get("id"))
        times = it.get("times", it.get("count", None))
        if sid is None:
            raise PlanError("%s: 字典写法必须有 'skill'" % who)
        return (_times(times, who), _sid(sid, who))
    if isinstance(it, (list, tuple)) and len(it) == 2:
        return (_times(it[0], who), _sid(it[1], who))
    raise PlanError("%s: 看不懂的写法 %r (支持 37383 / (5,37381) / {'skill':..,'times':..})"
                    % (who, it))


def normalize(rotation, who="") -> tuple:
    """把各种写法统一成 [(次数 or None, 技能id), ...]; 返回 (steps, 警告列表)."""
    warn = []
    if rotation is None:
        raise PlanError("%s: rotation 不能为空" % who)
    if isinstance(rotation, dict) and ("rotation" in rotation or "skills" in rotation):
        rotation = rotation.get("rotation", rotation.get("skills"))
    if isinstance(rotation, (list, tuple)) and not _is_pair(rotation):
        items = list(rotation)
    else:
        items = [rotation]
    if not items:
        raise PlanError("%s: rotation 是空的" % who)
    steps = [_one_step(it, "%s 第%d项" % (who, k + 1)) for k, it in enumerate(items)]
    for k, (times, _s) in enumerate(steps[:-1]):
        if times is None:
            warn.append("%s: 第%d项是'一直用', 它后面的 %d 项永远轮不到, 已忽略"
                        % (who, k + 1, len(steps) - k - 1))
            steps = steps[:k + 1]
            break
    return steps, warn


# ------------------------------ 加载文件 ------------------------------
def available() -> list:
    """列出本目录下可用的出招模式文件名(不含 .py)."""
    try:
        return sorted(f[:-3] for f in os.listdir(PLAN_DIR)
                      if f.endswith(".py") and not f.startswith("_"))
    except OSError:
        return []


def _resolve(name) -> str:
    if not name:
        raise PlanError("没有指定出招模式文件名")
    if os.path.sep in str(name) or str(name).endswith(".py"):
        p = os.path.abspath(str(name))
        if os.path.isfile(p):
            return p
    p = os.path.join(PLAN_DIR, str(name) + ".py")
    if os.path.isfile(p):
        return p
    raise PlanError("找不到出招模式文件 %r; 本目录现有: %s" % (name, available() or "(空)"))


def load(name) -> tuple:
    """读一个出招模式文件. 返回 (plans, meta).

    plans: {物种id: [(次数 or None, 技能id), ...]}
    meta:  {"path":..., "notes":{物种id:备注}, "warnings":[...]}
    """
    path = _resolve(name)
    spec = importlib.util.spec_from_file_location("_petplan_%x" % (abs(hash(path)) & 0xFFFFFFFF), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    raw = getattr(mod, "PLANS", None)
    if not isinstance(raw, dict) or not raw:
        raise PlanError("%s 里必须有一个非空的 PLANS 字典" % path)
    plans, notes, warns = {}, {}, []
    for pid, rot in raw.items():
        if not isinstance(pid, int) or pid <= 0:
            raise PlanError("%s: 键必须是精灵物种 id(正整数), 得到 %r" % (path, pid))
        who = "精灵 %d" % pid
        if isinstance(rot, dict) and rot.get("note"):
            notes[int(pid)] = str(rot["note"])
        steps, w = normalize(rot, who)
        plans[int(pid)] = steps
        warns.extend(w)
    return plans, {"path": path, "notes": notes, "warnings": warns}


# ------------------------------ 运行器 ------------------------------
class Runner:
    """按出招模式给出每一手技能. 计数按 (本场对战, 精灵物种id) 独立累计."""

    def __init__(self, plans: dict, meta: dict = None):
        self.plans = dict(plans or {})
        self.meta = dict(meta or {})
        self.counters = {}

    def reset(self):
        """一场新对战开始时调用."""
        self.counters = {}

    def used(self, pet_id) -> int:
        return int(self.counters.get(int(pet_id), 0))

    def has(self, pet_id) -> bool:
        try:
            return int(pet_id) in self.plans
        except (TypeError, ValueError):
            return False

    def next_skill(self, pet_id, avail=None, fallback=None):
        """给出这只精灵这一手该用的技能 id; 没配置就返回 fallback / 第一个可用技能."""
        try:
            pid = int(pet_id)
        except (TypeError, ValueError):
            pid = -1
        steps = self.plans.get(pid)
        if not steps:
            if fallback is not None:
                return fallback
            return (list(avail)[0] if avail else None)
        i = self.used(pid)
        for times, sid in steps:
            if times is None:
                return sid
            if i < times:
                return sid
            i -= times
        return steps[-1][1]          # 步骤走完 -> 重复最后一个技能

    def advance(self, pet_id):
        """真正出过一手之后调用, 推进该精灵的计数."""
        try:
            pid = int(pet_id)
        except (TypeError, ValueError):
            return
        self.counters[pid] = self.counters.get(pid, 0) + 1

    def describe(self, with_names=True) -> str:
        lines = []
        src = self.meta.get("path")
        lines.append("出招模式%s:" % ("(来自 %s)" % src if src else ""))
        for pid in sorted(self.plans):
            nm = (" " + pet_name(pid)) if with_names else ""
            segs = []
            for times, sid in self.plans[pid]:
                sn = (" " + skill_name(sid)) if with_names else ""
                segs.append("%d%s ×%s" % (sid, sn, "∞" if times is None else times))
            note = self.meta.get("notes", {}).get(pid)
            lines.append("  %-6d%-10s: %s%s" % (pid, nm, "  ->  ".join(segs),
                                                ("   # " + note) if note else ""))
        for w in self.meta.get("warnings", []):
            lines.append("  [警告] %s" % w)
        return "\n".join(lines)


def load_runner(name) -> Runner:
    """一步到位: 读文件 -> Runner."""
    plans, meta = load(name)
    return Runner(plans, meta)
