"""Check every url and links[].url in catalog/sources/*.json. Writes cache/link_report.json.
Usage: python scripts/check_links.py [id ...]   (default: all)"""
import asyncio, json, sys
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


async def check(client, sem, sid, url):
    async with sem:
        try:
            async with client.stream("GET", url) as r:  # stream: headers only, body never read
                return {"id": sid, "url": url, "status": r.status_code, "final": str(r.url)}
        except Exception as ex:
            return {"id": sid, "url": url, "status": None, "error": f"{type(ex).__name__}: {ex}"[:200]}


async def main(only):
    items = []
    for f in sorted((ROOT / "catalog/sources").glob("*.json")):
        for e in json.loads(f.read_text("utf-8"))["sources"]:
            if only and e["id"] not in only:
                continue
            items += [(e["id"], e["url"])] + [(e["id"], ln["url"]) for ln in e.get("links", [])]
    sem = asyncio.Semaphore(16)
    async with httpx.AsyncClient(headers={"User-Agent": UA, "Accept": "*/*"}, follow_redirects=True,
                                 timeout=25, verify=True) as c:
        res = await asyncio.gather(*(check(c, sem, i, u) for i, u in items))
    bad = [r for r in res if r["status"] is None or r["status"] >= 400]
    out = {"checked": len(res), "failed": len(bad), "failures": bad}
    (ROOT / "cache").mkdir(exist_ok=True)
    p = ROOT / "cache/link_report.json"
    p.write_text(json.dumps(out, indent=1), "utf-8")
    print(f"checked {len(res)}, failed {len(bad)} -> {p}")


if __name__ == "__main__":
    asyncio.run(main(set(sys.argv[1:])))
