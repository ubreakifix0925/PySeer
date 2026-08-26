from PySeer import Seer
s = Seer()
s.send(41597,[1])
favor = s.get_recv_value(46046,[1,1707],1)
print("当前好感度：",favor)

print("开始喂食")
s.send(41597,[2])
cnt = 0
while s.get_recv_value(46046,[1,1707],1)>favor:
    cnt += 1
    favor = s.get_recv_value(46046,[1,1707],1)
    s.send(41597,[2])
print("喂食",cnt,"次")
print("喂食完成")
favor = s.get_recv_value(46046,[1,1707],1)
print("当前好感度：",favor)

print("开始打扫")
s.send(41597,[3])
cnt = 0
while s.get_recv_value(46046,[1,1707],1)>favor:
    cnt += 1
    favor = s.get_recv_value(46046,[1,1707],1)
    s.send(41597,[3])
print("打扫",cnt,"次")
print("打扫完成")
favor = s.get_recv_value(46046,[1,1707],1)
print("当前好感度：",favor)

print("开始治病")
s.send(41597,[4])
cnt = 0
while s.get_recv_value(46046,[1,1707],1)>favor:
    cnt += 1
    favor = s.get_recv_value(46046,[1,1707],1)
    s.send(41597,[4])
print("治病",cnt,"次")
print("治病完成")
favor = s.get_recv_value(46046,[1,1707],1)
print("当前好感度：",favor)


if favor>=100:
    s.send(41597,[5])
    s.send(41597,[1])
    print("好感度达到100，兑换护士执照")
else:
    print("好感度不足100")

lics = s.get_item_count(1703929)
print("当前拥有护士执照",lics,"个")



