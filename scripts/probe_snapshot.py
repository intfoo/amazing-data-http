"""带超时测试 query_snapshot，确认能否作为盘后 fallback。"""
import json, os, sys, threading, time
cfg = json.load(open(os.path.join(os.path.dirname(__file__), "..", "local.config.json")))
import AmazingData as ad
ad.login(username=cfg["AMAZINGDATA_USERNAME"], password=cfg["AMAZINGDATA_PASSWORD"],
         host=cfg["AMAZINGDATA_HOST"], port=int(cfg["AMAZINGDATA_PORT"]))
print("[1] login OK", flush=True)

base = ad.BaseData()
calendar = base.get_calendar()
today = calendar[-1]
print(f"[2] today={today}", flush=True)

md = ad.MarketData(calendar)

result_box = [None, None]  # [result, error]

def do_query():
    try:
        r = md.query_snapshot(["000001.SZ"], begin_date=today, end_date=today)
        result_box[0] = r
    except Exception as e:
        result_box[1] = f"{type(e).__name__}: {e}"

t = threading.Thread(target=do_query, daemon=True)
t.start()
t.join(timeout=12)

if t.is_alive():
    print(f"[3] query_snapshot TIMED OUT after 25s (still running)", flush=True)
elif result_box[1]:
    print(f"[3] query_snapshot ERROR: {result_box[1]}", flush=True)
else:
    r = result_box[0]
    print(f"[3] query_snapshot OK type={type(r).__name__}", flush=True)
    if isinstance(r, dict):
        for k, v in r.items():
            print(f"  key={k!r} value_type={type(v).__name__}", flush=True)
            if hasattr(v, 'columns'):
                print(f"    df shape={v.shape} cols={list(v.columns)[:15]}", flush=True)
                if len(v) > 0:
                    print(f"    row0={dict(v.iloc[0])}", flush=True)
            elif hasattr(v, '__len__'):
                print(f"    len={len(v)} repr={repr(v)[:300]}", flush=True)
            else:
                print(f"    repr={repr(v)[:300]}", flush=True)
    elif hasattr(r, 'columns'):
        print(f"  df shape={r.shape} cols={list(r.columns)[:12]}", flush=True)

try:
    ad.logout(username=cfg["AMAZINGDATA_USERNAME"])
except: pass
print("done", flush=True)
