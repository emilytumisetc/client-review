"""Two-way sync between the ETC Video Content Studio and the client review site.

Run from the repo root:
  python3 -I tools/sync.py --passcode CODE --videos DIR --sheet SHEET.txt --assets DIR --out studio_updates.json

--videos  folder of studio video records (ArtifactData list with out_dir, collection "videos")
--sheet   text of the "Deb Content Review (ETC)" Google Sheet (Drive read_file_content fileContent)
--assets  folder holding studio images named <assetId>.<ext> (poster + graphics of queued videos)
Writes content.enc.json and img/*.enc.json in place, and a JSON list of studio updates
[{"doc_id": ..., "version": ..., "data": {...}}] to apply with ArtifactData update.
"""
import argparse, base64, glob, json, os, re, sys, time
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

ITER = 200_000

def _key(code, salt):
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(code.encode())

def encrypt(code, data, kind=None):
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(code, salt)).encrypt(iv, data, None)
    b = lambda x: base64.b64encode(x).decode()
    out = {"v": 1, "iter": ITER, "salt": b(salt), "iv": b(iv), "ct": b(ct)}
    if kind: out["type"] = kind
    return out

def decrypt(code, env):
    d = lambda s: base64.b64decode(s)
    return AESGCM(_key(code, d(env["salt"]))).decrypt(d(env["iv"]), d(env["ct"]), None)

# ---------- sheet parsing (markdown tables from Drive) ----------
def parse_sheet(text):
    tabs, cur, rows = {}, None, []
    for line in text.splitlines():
        m = re.match(r"### Sheet Name:\s*(.+)", line)
        if m:
            if cur: tabs[cur] = rows
            cur, rows = m.group(1).strip(), []
            continue
        if cur and line.startswith("### Table Columns"):
            tabs[cur] = rows; cur = None; continue
        if cur and line.startswith("|") and not re.match(r"^\|\s*:?-", line):
            cells = [c.strip().replace("\\_", "_").replace("\\|", "|").replace("\\*", "*") for c in re.split(r"(?<!\\)\|", line)[1:-1]]
            rows.append(cells)
    if cur: tabs[cur] = rows
    def as_dicts(rows):
        rows = [r for r in rows if any(r)]
        if not rows: return []
        head = rows[0]
        return [dict(zip(head, r)) for r in rows[1:]]
    return {k: as_dicts(v) for k, v in tabs.items()}

def num(x):
    try: return int(float(x))
    except Exception: return 0

# ---------- studio -> review items ----------
LABEL = {"linkedin": "Long video caption", "instagram": "Long video caption"}
def piece_keys(c, g):
    keys = []
    if c.get("linkedin"): keys.append("linkedin")
    if c.get("instagram"): keys.append("instagram")
    keys += [f"reel{i}" for i, _ in enumerate(c.get("reels") or [])]
    keys += [f"story{i}" for i, _ in enumerate(c.get("stories") or [])]
    if c.get("statGraphic") and g.get("stat", True): keys.append("stat")
    if c.get("quoteCard") and g.get("quote", True): keys.append("quote")
    if (c.get("carousel") or {}).get("slides") and g.get("carousel", False): keys.append("carousel")
    return keys

def build_piece(v, k, img_for):
    c = v.get("content") or {}
    sched = (v.get("schedule") or {}).get(k) or {}
    p = {"key": k, "date": sched.get("date", ""), "time": sched.get("time", "")}
    if k == "linkedin":
        p.update(group="Long video", platform="LinkedIn", label="Long video caption", fields=[{"label": "", "value": c["linkedin"].get("caption", "")}])
    elif k == "instagram":
        p.update(group="Long video", platform="Instagram", label="Long video caption", fields=[{"label": "", "value": c["instagram"].get("caption", "")}])
    elif k.startswith("reel"):
        r = c["reels"][int(k[4:])]
        cut = f"Starts at: \"{r.get('startLine','')}\"\nEnds at: \"{r.get('endLine','')}\""
        p.update(group="Reels", platform="Instagram Reel", label=r.get("angle") or f"Reel {int(k[4:])+1}",
                 fields=[{"label": "On-screen text", "value": r.get("onScreenText", "")}, {"label": "Caption", "value": r.get("caption", "")}, {"label": "Where the clip is cut", "value": cut}])
        if v.get("driveUrl"): p["video"] = v["driveUrl"]; p["linkLabel"] = "Play the full video"
    elif k.startswith("story"):
        s = c["stories"][int(k[5:])]; st = s.get("sticker") or {}
        sticker = ""
        if st.get("type"):
            sticker = f"{st['type'].title()} sticker: {st.get('prompt','')}" + (f" ({' / '.join(st.get('options') or [])})" if st.get("options") else "")
        p.update(group="Stories", platform="Instagram Story", label=f"Story frame {int(k[5:])+1}", fields=[{"label": "Frame text", "value": s.get("text", "")}] + ([{"label": "Sticker", "value": sticker}] if sticker else []))
    elif k == "stat":
        s = c["statGraphic"]
        p.update(group="Static posts", platform="LinkedIn and Instagram", label="Stat graphic", fields=[{"label": "On the graphic", "value": "\n".join(x for x in [s.get("kicker",""), s.get("headline",""), s.get("supportingLine","")] if x)}, {"label": "Caption", "value": s.get("caption", "")}])
    elif k == "quote":
        q = c["quoteCard"]
        p.update(group="Static posts", platform="LinkedIn and Instagram", label="Quote card", fields=[{"label": "On the graphic", "value": "“" + q.get("quote", "") + "”"}, {"label": "Caption", "value": q.get("caption", "")}])
    elif k == "carousel":
        cr = c["carousel"]
        slides = "\n\n".join(f"Slide {i+1}: {s.get('headline','')}" + (f"\n{s['body']}" if s.get("body") else "") for i, s in enumerate(cr.get("slides") or []))
        p.update(group="Static posts", platform="LinkedIn and Instagram", label="Carousel", fields=[{"label": "Slides", "value": slides}, {"label": "Caption", "value": cr.get("caption", "")}])
    imgs = img_for(k)
    if imgs: p["images"] = imgs
    return p

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--passcode", required=True); ap.add_argument("--videos", required=True)
    ap.add_argument("--sheet", required=True); ap.add_argument("--assets", default="")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    code = a.passcode
    content = json.loads(decrypt(code, json.load(open("content.enc.json"))))
    items = {it["id"]: it for it in content.get("items", [])}

    videos = {}
    for f in glob.glob(os.path.join(a.videos, "**", "*.json"), recursive=True):
        d = json.load(open(f)); vid = d.get("id") or os.path.splitext(os.path.basename(f))[0]
        videos[vid] = {"data": d.get("data", d), "version": d.get("version")}

    def asset_file(aid):
        for ext in ("jpg", "jpeg", "png", "webp"):
            p = os.path.join(a.assets, f"{aid}.{ext}")
            if a.assets and os.path.exists(p): return p, ext
        hits = glob.glob(os.path.join(a.assets, f"{aid}*")) if a.assets else []
        return (hits[0], hits[0].rsplit(".", 1)[-1]) if hits else (None, None)

    def enc_image(aid, name):
        path, ext = asset_file(aid)
        if not path: return None
        os.makedirs("img", exist_ok=True)
        kind = "image/png" if ext == "png" else "image/jpeg"
        out = f"img/{name}.enc.json"
        json.dump(encrypt(code, open(path, "rb").read(), kind), open(out, "w"))
        return out

    updates, now, sent, removed, missing = [], int(time.time() * 1000), [], [], []
    for vid, rec in sorted(videos.items(), key=lambda x: x[1]["data"].get("order", 0)):
        v = rec["data"]
        st = v.get("clientState")
        if st == "remove":
            if vid in items: items[vid]["hidden"] = True
            removed.append(vid)
            updates.append({"doc_id": vid, "version": rec["version"], "data": {"clientState": "removed", "clientSyncedAt": now}})
            continue
        if st != "queued" or not v.get("content"):
            continue
        c = v["content"]; g = {"stat": True, "quote": True, "carousel": False, **(v.get("graphics") or {})}
        keys = piece_keys(c, g)
        ps = v.get("pieceStatus") or {}
        approved = [k for k in keys if ps.get(k) in ("ETC approved", "Reviewer approved", "Deb approved")]
        keys = approved or keys
        gi = v.get("graphicsImages") or []
        def img_for(k):
            if k not in ("stat", "quote", "carousel"): return []
            out = []
            for n, im in enumerate(x for x in gi if x.get("piece") == k):
                f = enc_image(im["id"], f"{vid}-{k}-{n+1}")
                if f: out.append({"file": f, "alt": im.get("label", "")})
                else: missing.append(im["id"])
            return out
        item = {"id": vid, "order": v.get("order", 0), "title": f"Video {v.get('order')}: {v.get('title','')}" if v.get("kind") == "finished" else v.get("title", ""),
                "topic": v.get("topic", ""), "videoUrl": v.get("driveUrl", ""), "graphicsUrl": "", "sentAt": now,
                "note": v.get("clientNote", ""), "pieces": [build_piece(v, k, img_for) for k in keys]}
        if v.get("posterId"):
            f = enc_image(v["posterId"], f"{vid}-poster")
            if f: item["poster"] = f
            else: missing.append(v["posterId"])
        items[vid] = item
        sent.append(vid)
        updates.append({"doc_id": vid, "version": rec["version"], "data": {"clientState": "sent", "clientSentAt": now, "clientSyncedAt": now, "status": "In review"}})

    # schedule changes for items already on the site
    for vid, it in items.items():
        if vid in sent or vid not in videos: continue
        sch = videos[vid]["data"].get("schedule") or {}
        for p in it.get("pieces", []):
            s = sch.get(p["key"]) or {}
            if s.get("date"): p["date"], p["time"] = s.get("date", ""), s.get("time", "")

    content["items"] = sorted(items.values(), key=lambda x: x.get("order", 0))
    content["updatedAt"] = now
    json.dump(encrypt(code, json.dumps(content).encode()), open("content.enc.json", "w"))

    # review -> studio
    tabs = parse_sheet(open(a.sheet).read())
    dec = {r.get("key"): r for r in tabs.get("Decisions", []) if r.get("key")}
    cmts = tabs.get("Comments", [])
    for vid, it in items.items():
        if vid not in videos or it.get("hidden"): continue
        cs, pstat = {}, {}
        for p in it.get("pieces", []):
            r = dec.get(f"{vid}--{p['key']}", {})
            entry = {"label": p.get("label", "")}
            if r.get("reviewer_status"): entry["reviewer"] = {"status": r["reviewer_status"], "note": r.get("reviewer_note", ""), "by": r.get("reviewer_name", ""), "at": num(r.get("reviewer_at"))}
            if r.get("deb_status"): entry["deb"] = {"status": r["deb_status"], "note": r.get("deb_note", ""), "by": r.get("deb_name", ""), "at": num(r.get("deb_at"))}
            entry["comments"] = [{"by": x.get("name", ""), "text": x.get("text", ""), "at": num(x.get("at"))} for x in cmts if x.get("videoId") == vid and x.get("pieceKey") == p["key"]]
            cs[p["key"]] = entry
            if entry.get("deb", {}).get("status") == "approved": pstat[p["key"]] = "Deb approved"
            elif entry.get("reviewer", {}).get("status") == "approved": pstat[p["key"]] = "Reviewer approved"
        data = {"clientStatus": cs, "clientSyncedAt": now}
        if pstat: data["pieceStatus"] = pstat
        if cs and all(e.get("deb", {}).get("status") == "approved" for e in cs.values()): data["status"] = "Approved"
        u = next((u for u in updates if u["doc_id"] == vid), None)
        if u: u["data"].update(data)
        else: updates.append({"doc_id": vid, "version": videos[vid]["version"], "data": data})

    json.dump(updates, open(a.out, "w"), indent=1)
    print(json.dumps({"sent": sent, "removed": removed, "studio_updates": len(updates), "missing_images": missing}))

if __name__ == "__main__":
    main()
