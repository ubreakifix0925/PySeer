from seerlib import Seer
s = Seer()          # 运行时自动指向已登录后端 (无需在代码里硬编码地址)
pkt = s.recv(43706)       # ② 接收函数: 发 SEND + 等 RECV, 返回完整包体(Packet)
v = s.get_value(pkt, 0)                   # ③ 取值函数: 取包体第 0 个 int32
print(v)
s.set_bag([3512,3329,3463]) #换背包函数，把背包调整成指定阵容
