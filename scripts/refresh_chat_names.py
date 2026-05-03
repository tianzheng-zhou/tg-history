"""一次性脚本：调用 /api/telegram/refresh-names 修复 chat_name == chat_id 的脏数据。

用法：venv\\Scripts\\python scripts\\refresh_chat_names.py
"""
import json
import time
import urllib.request


def main(base_url: str = "http://127.0.0.1:8000") -> None:
    t = time.time()
    req = urllib.request.Request(
        f"{base_url}/api/telegram/refresh-names", method="POST"
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read())

    print(f"Done in {time.time() - t:.1f}s")
    print(f"Checked: {data['checked']}, Updated: {data['updated']}")
    print()
    for it in data["items"]:
        cid = it["chat_id"]
        if it["status"] == "ok":
            print(f"  OK   {cid:>14}  =>  {it['new_name']}")
        elif it["status"] == "error":
            print(f"  ERR  {cid:>14}  :  {it['error']}")
        else:
            print(f"  --   {cid:>14}  (unchanged)")


if __name__ == "__main__":
    main()
