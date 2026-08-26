"""对战体(Battle)示例脚本 — 用 seerlib 自动驱动一场对战.

前置: 后端(webui)已登录游戏账号. 把本脚本放进 app/scripts/ 即可在 WebUI「脚本」页运行;
或直接命令行: ``PYTHONPATH=app python3 app/scripts/battle_demo.py``.

核心: ``Battle("带cmdid的完整HEX包")`` 构造时即自动发送对战包并**等待进场**(失败抛 SeerError);
进入后**每个操作自动对应一回合**: 用技能/用道具/捕捉在发包后自动等到本回合结算(2505)才返回,
所以**不需要**手动写等待回合; 只有**死亡切换** ``change_pet`` 不消耗回合, 换上新精灵后可在
同一回合内继续执行一次操作. 收到结束包(2506)后 ``finished`` 置 True, 循环自动终止.

⚠️ 请把 ``BATTLE_HEX`` 换成**真实**的"带 cmdid 的完整 HEX 包"(例如挑战/进入对战的封包,
  从游戏抓包或已记录的封包得到). 下面是占位, 直接运行会因命令号无效而失败.
"""

from seerlib import Battle, SeerError

# 带 cmdid 的完整 HEX 包(作为对战进入输入). 示例结构 [len4][ver0x31][cmd4][uid4][res4][body].
# 实际请替换为你抓到的对战触发包(如挑战 Boss / 邀战), 下面的 0x00 只是长度错误的占位.
BATTLE_HEX = "0000001531000000010000000000000000"


def main():
    battle = Battle(BATTLE_HEX)          # 发送对战包 + 自动等待进场; 失败会抛 SeerError
    print(f"已进入对战 mode={battle.mode}")
    print(f"我方队伍: " + "、".join(f"{p.get('name')}(HP {p.get('hp')})" for p in battle.my_team))
    print(f"敌方队伍: " + "、".join(f"{p.get('name')}(HP {p.get('hp')})" for p in battle.other_team))

    round_no = 0
    while not battle.finished:
        my, other = battle.my, battle.other   # 双方当前出战精灵
        my_hp = (my or {}).get("hp") or 0
        # —— 任意复杂的判断结构(你按自己的策略改这里) ——
        if my_hp <= 0:
            # 当前精灵阵亡: 死亡切换(不消耗回合), 换上新精灵后再出招
            target = next((p for p in battle.my_team
                           if p.get("catchTime") != (my or {}).get("catchTime")
                           and (p.get("hp") or 0) > 0), None)
            battle.act(f"> 脚本: 死亡切换 → {target.get('name') if target else '?'}")
            battle.change_pet(target["id"])        # 换宠(2407, 传物种id, 后端从阵容查catchTime), 不消耗回合
            battle.use_skill(battle.skills[0])       # 同一回合内继续出招
        elif my_hp < 300:
            battle.act(f"> 脚本: 血量偏低, 用药(2406)")
            battle.use_item()                        # 用道具(消耗一回合)
        else:
            skill = (battle.skills or [None])[0]
            battle.act(f"> 脚本: 使用技能 {skill}")
            battle.use_skill(skill)                  # 用技能(消耗一回合)

        round_no += 1
        rnd = battle.round               # 刚才这一回合(2505)的解析结果
        first = (rnd or {}).get("first") or {}
        second = (rnd or {}).get("second") or {}
        print(f"回合{round_no}: 我方技能{first.get('skillID')} 造成{first.get('lostHP')}伤害 "
              f"剩余HP {first.get('remainHP')}/{first.get('maxHp')} | "
              f"敌方技能{second.get('skillID')} 造成{second.get('lostHP')}伤害 "
              f"剩余HP {second.get('remainHP')}/{second.get('maxHp')}")

    print(f"对战结束! finished={battle.finished} 共 {round_no} 回合")
    print("战报: " + " | ".join(r.get("msg", "") for r in battle.report))


if __name__ == "__main__":
    try:
        main()
    except SeerError as e:
        print(f"〔脚本中止〕{e}")
        raise SystemExit(1)
