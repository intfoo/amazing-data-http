"""第一性原理测试：主线程跑 SubscribeData.run()，验证回调是否触发。

之前探测在 daemon 子线程跑 run()，零回调。怀疑 SDK 回调依赖主线程。
本脚本在主线程跑 run()，daemon 子线程 8 秒后 os._exit 强制退出。
若主线程能收到回调 → 根因是"子线程不工作"，需改架构。
"""
import dataclasses
import os
import sys
import threading
import time


def _load_env_creds():
    """读 .env 注入 os.environ 并返回凭据 dict（替代 local.config.json）。"""
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
    return {
        "AMAZINGDATA_USERNAME": os.environ.get("AMAZINGDATA_USERNAME", ""),
        "AMAZINGDATA_PASSWORD": os.environ.get("AMAZINGDATA_PASSWORD", ""),
        "AMAZINGDATA_HOST": os.environ.get("AMAZINGDATA_HOST", ""),
        "AMAZINGDATA_PORT": os.environ.get("AMAZINGDATA_PORT", ""),
    }


cfg = _load_env_creds()

import AmazingData as ad
ad.login(username=cfg["AMAZINGDATA_USERNAME"], password=cfg["AMAZINGDATA_PASSWORD"],
         host=cfg["AMAZINGDATA_HOST"], port=int(cfg["AMAZINGDATA_PORT"]))
print("[1] login OK", flush=True)

from AmazingData.utils.constant import Period
print(f"[2] Period.snapshot.value = {Period.snapshot.value}", flush=True)

code_list = ["000001.SZ", "600000.SH", "600519.SH"]
print(f"[3] code_list = {code_list}", flush=True)

sub = ad.SubscribeData()
calls = []
struct_checked = [False]


@sub.register(code_list=code_list, period=Period.snapshot.value)
def onSnapshot(data, period):
    calls.append(data)
    if not struct_checked[0]:
        struct_checked[0] = True
        print(f"\n>>> CALLBACK #{len(calls)} type={type(data).__name__} period={period}", flush=True)
        print(f"    is_dataclass={dataclasses.is_dataclass(data)}", flush=True)
        if dataclasses.is_dataclass(data):
            fs = [f.name for f in dataclasses.fields(data)]
            print(f"    fields({len(fs)}): {fs}", flush=True)
            print(f"    sample: code={getattr(data,'code',None)} last={getattr(data,'last',None)} trade_time={getattr(data,'trade_time',None)}", flush=True)
        elif hasattr(data, "__dict__"):
            print(f"    __dict__ keys: {list(vars(data).keys())}", flush=True)
    else:
        print(f">>> CALLBACK #{len(calls)} code={getattr(data,'code','?')}", flush=True)


def killer():
    time.sleep(8)
    sys.stdout.flush()
    print(f"\n[RESULT] {len(calls)} callbacks in 8s (run in MAIN thread)", flush=True)
    if len(calls) == 0:
        print("根因确认: 主线程也零回调 → 非子线程问题，是 SDK 不推送/权限/非交易时段", flush=True)
    else:
        print("根因反转: 主线程有回调 → 之前 daemon 子线程是根因，需改架构让 run() 在主线程", flush=True)
    sys.stdout.flush()
    os._exit(0)


threading.Thread(target=killer, daemon=True).start()
print("[4] sub.run() in MAIN thread (blocking, will be killed after 8s)...", flush=True)
try:
    sub.run()
except SystemExit:
    pass
except Exception as e:
    print(f"[4] run() exception: {type(e).__name__}: {e}", flush=True)
