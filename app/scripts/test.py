import time
from seerlib import Battle
from seerlib import Seer
s = Seer()
print("test")
s.set_bag([3512,3329,3463])
time.sleep(2)
battle = Battle("00000015310000A0A9383934A3000002B700001A48")   # 发送对战包 + 自动进场; 无法进入时抛 SeerError
battle.use_skill(31505)
battle.change_pet(3329)
battle.use_skill(19314)
battle.change_pet(3463)
battle.use_skill(31252)
    
    
