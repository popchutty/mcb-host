from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sciscope.config import Channel, Profile
from sciscope.motor import official_profile
from sciscope.dyno import dyno_profile

root = Path(__file__).resolve().parents[1]
profiles = root / "profiles"
profiles.mkdir(exist_ok=True)
Profile().save(profiles / "sci_default.json")
official_profile().save(profiles / "foc_single.json")
official_profile(True).save(profiles / "foc_q28.json")
dyno_profile().save(profiles / "dyno_single.json")
dyno_profile(True).save(profiles / "dyno_q28.json")
multi = Profile()
multi.name = "Generic 8 channels / 20 kHz"
multi.rx.channels = 8
multi.rx.channel_info = [Channel(f"Channel{i + 1}") for i in range(8)]
multi.save(profiles / "generic_8ch.json")
print(profiles)
