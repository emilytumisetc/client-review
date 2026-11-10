"""Two-way sync between the ETC Video Content Studio and the client review site.

Run from the repo root:
  python3 -I tools/sync.py --passcode CODE --videos DIR --sheet SHEET.txt --assets DIR --out studio_updates.json

--videos  folder of studio video records (ArtifactData list with out_dir, collection "videos")
--sheet   text of the "Deb Content Review (ETC)" Google Sheet (Drive read_file_content fileContent)
--amy-passcode  ETC-only passcode for Amy's edit page (edit.html); enables the Amy step
--assets  folder holding studio images named <assetId>.<ext> (poster, clip thumbnails in clipPosters, and graphics of queued videos)
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
    if c.get("statGraphic2") and g.get("stat2", True): keys.append("stat2")
    if c.get("quoteCard") and g.get("quote", True): keys.append("quote")
    if (c.get("carousel") or {}).get("slides") and g.get("carousel", False): keys.append("carousel")
    return keys

def full_len(v):
    sec = (v.get("opus") or {}).get("durationSec") or 0
    if not sec and (v.get("opus") or {}).get("projectId"):  # only OpusClip transcripts carry real timestamps
        m = re.findall(r"(\d{1,2}):(\d{2}) - (\d{1,2}):(\d{2})", v.get("transcript") or "")
        if m: sec = int(m[-1][2]) * 60 + int(m[-1][3])
    return f"{sec // 60}:{sec % 60:02d}" if sec else ""

def long_video(v, p, k):
    url = (v.get("longVideos") or {}).get(k) or v.get("driveUrl")
    if url:
        p["video"] = url
        ln = full_len(v) if url == v.get("driveUrl") else ""
        p["linkLabel"] = "Play the full video" + (f" · {ln}" if ln else "")

def build_piece(v, k, img_for, poster_for=None):
    c = v.get("content") or {}
    sched = (v.get("schedule") or {}).get(k) or {}
    p = {"key": k, "date": sched.get("date", ""), "time": sched.get("time", "")}
    if k == "linkedin":
        p.update(group="Long video", platform="LinkedIn", label="Long video caption", fields=[{"label": "", "value": c["linkedin"].get("caption", "")}])
        long_video(v, p, k)
    elif k == "instagram":
        p.update(group="Long video", platform="Instagram", label="Long video caption", fields=[{"label": "", "value": c["instagram"].get("caption", "")}])
        long_video(v, p, k)
    elif k.startswith("reel"):
        r = c["reels"][int(k[4:])]
        cut = f"Starts at: \"{r.get('startLine','')}\"\nEnds at: \"{r.get('endLine','')}\""
        # Client-facing: no internal angle notes or cut/fact-check notes
        p.update(group="Reels", platform="Instagram Reel", label=f"Reel {int(k[4:])+1}",
                 fields=[{"label": "On-screen text", "value": r.get("onScreenText", "")}, {"label": "Caption", "value": r.get("caption", "")}])
        if r.get("videoUrl"): p["video"] = r["videoUrl"]; p["linkLabel"] = "Play the clip"
        elif v.get("driveUrl"): p["video"] = v["driveUrl"]; p["linkLabel"] = "Play the full video"
        p["fields"] = [f for f in p["fields"] if f["value"]]
        cp = (v.get("clipPosters") or {}).get(str(r.get("opusClipId") or ""))
        if cp and cp.get("id") and poster_for:
            f = poster_for(cp["id"], f"{v.get('_id','v')}-{k}-poster")
            if f: p["poster"] = f
    elif k.startswith("story"):
        s = c["stories"][int(k[5:])]; st = s.get("sticker") or {}
        sticker = ""
        if st.get("type"):
            sticker = f"{st['type'].title()} sticker: {st.get('prompt','')}" + (f" ({' / '.join(st.get('options') or [])})" if st.get("options") else "")
        p.update(group="Stories", platform="Instagram Story", label=f"Story frame {int(k[5:])+1}", fields=[{"label": "Frame text", "value": s.get("text", "")}] + ([{"label": "Sticker", "value": sticker}] if sticker else []))
    elif k in ("stat", "stat2"):
        s = c["statGraphic" if k == "stat" else "statGraphic2"]
        caps = ([{"label": "LinkedIn caption", "value": s.get("caption", "")}, {"label": "Instagram caption", "value": s.get("captionInstagram", "")}]
                if s.get("captionInstagram") is not None else [{"label": "Caption", "value": s.get("caption", "")}])
        p.update(group="Static posts", platform="LinkedIn and Instagram", label="Stat graphic" + (" 2" if k == "stat2" else ""), fields=[{"label": "On the graphic", "value": "\n".join(x for x in [s.get("kicker",""), s.get("headline",""), s.get("supportingLine","")] if x)}] + caps)
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

# ---------- Amy (ETC editor) step: internal.enc.json, never visible to AARP ----------
def amy_fields(c, k):
    if k in ("linkedin", "instagram"):
        return [{"label": "Caption", "path": [k, "caption"], "value": c[k].get("caption", "")}]
    if k.startswith("reel"):
        i = int(k[4:]); r = c["reels"][i]
        return [{"label": "On-screen text", "path": ["reels", i, "onScreenText"], "value": r.get("onScreenText", "")},
                {"label": "Caption", "path": ["reels", i, "caption"], "value": r.get("caption", "")}]
    if k.startswith("story"):
        i = int(k[5:]); st = c["stories"][i]
        f = [{"label": "Frame text", "path": ["stories", i, "text"], "value": st.get("text", "")}]
        if (st.get("sticker") or {}).get("prompt"):
            f.append({"label": "Sticker question", "path": ["stories", i, "sticker", "prompt"], "value": st["sticker"]["prompt"]})
        return f
    if k in ("stat", "stat2"):
        prop = "statGraphic" if k == "stat" else "statGraphic2"; g = c[prop]
        if g.get("captionInstagram") is not None:
            return [{"label": "On the graphic", "path": [prop, "headline"], "value": g.get("headline", "")},
                    {"label": "LinkedIn caption", "path": [prop, "caption"], "value": g.get("caption", "")},
                    {"label": "Instagram caption", "path": [prop, "captionInstagram"], "value": g.get("captionInstagram", "")}]
        return [{"label": "On the graphic", "path": [prop, "headline"], "value": g.get("headline", "")},
                {"label": "Caption", "path": [prop, "caption"], "value": g.get("caption", "")}]
    if k == "quote":
        q = c["quoteCard"]
        return [{"label": "Quote on the graphic", "path": ["quoteCard", "quote"], "value": q.get("quote", "")},
                {"label": "Caption", "path": ["quoteCard", "caption"], "value": q.get("caption", "")}]
    if k == "carousel":
        cr = c["carousel"]
        f = []
        for i, sl in enumerate(cr.get("slides") or []):
            f.append({"label": f"Slide {i+1} headline", "path": ["carousel", "slides", i, "headline"], "value": sl.get("headline", "")})
            if sl.get("body"): f.append({"label": f"Slide {i+1} text", "path": ["carousel", "slides", i, "body"], "value": sl["body"]})
        f.append({"label": "Caption", "path": ["carousel", "caption"], "value": cr.get("caption", "")})
        return f
    return []

AMY_LABEL = {"linkedin": ("Long video", "LinkedIn", "Long video caption"), "instagram": ("Long video", "Instagram", "Long video caption"),
             "stat": ("Static posts", "LinkedIn and Instagram", "Stat graphic"), "stat2": ("Static posts", "LinkedIn and Instagram", "Stat graphic 2"), "quote": ("Static posts", "LinkedIn and Instagram", "Quote card"),
             "carousel": ("Static posts", "LinkedIn and Instagram", "Carousel")}

def amy_promote(videos, tabs, now):
    """Amy approved a post on her Edit Desk: put her edits into the studio copy and send the post to AARP.
    Runs before the AARP site is built. Changes are made in memory and returned as studio updates."""
    edits = [x for x in tabs.get("Edits", []) if x.get("videoId")]
    out = {}
    for vid, rec in videos.items():
        v = rec["data"]; c = v.get("content")
        if not c or v.get("amyState") not in ("sent", "back"): continue
        sent_at = v.get("amySentAt") or 0
        mine = [x for x in edits if x.get("videoId") == vid and num(x.get("at")) > sent_at]
        appr = {}
        for x in sorted(mine, key=lambda x: num(x.get("at"))):
            if x.get("field") == "__approved": appr[x.get("pieceKey", "")] = (x.get("text") == "yes", num(x.get("at")))
        promoted = dict(v.get("amyPromoted") or {})
        ps = dict(v.get("pieceStatus") or {})
        old = json.loads(json.dumps(c)); changed = False; sent_keys = []
        for k, (yes, at) in appr.items():
            if not yes or ps.get(k) in ("Skip", "Pulled back"): continue
            fl = amy_fields(c, k)
            fedits = [x for x in mine if x.get("pieceKey") == k and x.get("field") not in ("__approved", "__note")]
            last = max([at] + [num(x.get("at")) for x in fedits])
            if promoted.get(k, 0) >= last: continue  # already sent this version
            for x in sorted(fedits, key=lambda x: num(x.get("at"))):
                try: pth = json.loads(x.get("path") or "[]")
                except Exception: pth = []
                if not pth: pth = next((f["path"] for f in fl if f["label"] == x.get("field")), [])
                if not pth: continue
                o = c
                try:
                    for seg in pth[:-1]: o = o[seg]
                    if o[pth[-1]] != x.get("text", ""): o[pth[-1]] = x.get("text", ""); changed = True
                except Exception: pass
            if ps.get(k) not in ("Reviewer approved", "Deb approved", "Sent to AARP"): ps[k] = "Amy approved"
            promoted[k] = last; sent_keys.append(k)
        if not sent_keys: continue
        aa = [a for a in (v.get("amyApproved") or []) if isinstance(a, dict)]
        have = {a.get("key") for a in aa}
        for k in sent_keys:
            if k not in have: aa.append({"key": k, "by": "Amy", "at": promoted[k]})
        v["amyApproved"] = aa; v["pieceStatus"] = ps; v["amyPromoted"] = promoted
        if v.get("clientState") != "remove": v["clientState"] = "queued"
        d = {"amyApproved": aa, "pieceStatus": ps, "amyPromoted": promoted, "autoSentToAarp": {"at": now, "keys": sent_keys}}
        if changed:
            d["content"] = c; d["prevContent"] = old; d["amyAppliedAt"] = now; rec["orig_content"] = old
        out[vid] = d
    return out

def amy_sync(acode, videos, tabs, updates, asset_file, now):
    os.makedirs("img-etc", exist_ok=True)
    def enc(aid, name):
        path, ext = asset_file(aid)
        if not path: return None
        out = f"img-etc/{name}.enc.json"
        json.dump(encrypt(acode, open(path, "rb").read(), "image/png" if ext == "png" else "image/jpeg"), open(out, "w"))
        return out
    try: internal = json.loads(decrypt(acode, json.load(open("internal.enc.json"))))
    except Exception: internal = {"items": []}
    items = {it["id"]: it for it in internal.get("items", [])}
    for vid, rec in videos.items():
        v = rec["data"]; st = v.get("amyState")
        if st == "done" and vid in items: items[vid]["hidden"] = True; continue
        if st != "queued" or not v.get("content"): continue
        c = v["content"]; g = {"stat": True, "quote": True, "carousel": False, **(v.get("graphics") or {})}
        keys = [k for k in piece_keys(c, g) if (v.get("pieceStatus") or {}).get(k) != "Skip"]
        chosen = [k for k in keys if (v.get("pieceStatus") or {}).get(k) == "With Amy"]
        keys = chosen or keys
        gi = v.get("graphicsImages") or []
        pieces = []
        for k in keys:
            if k.startswith("reel"): grp, plat, lab = "Reels", "Instagram Reel", f"Reel {int(k[4:])+1}"
            elif k.startswith("story"): grp, plat, lab = "Stories", "Instagram Story", f"Story frame {int(k[5:])+1}"
            else: grp, plat, lab = AMY_LABEL[k]
            p = {"key": k, "group": grp, "platform": plat, "label": lab, "fields": amy_fields(c, k)}
            imgs = [enc(im["id"], f"{vid}-{k}-{n+1}") for n, im in enumerate(x for x in gi if x.get("piece") == k)]
            if any(imgs): p["images"] = [{"file": f} for f in imgs if f]
            if k.startswith("reel") and (c["reels"][int(k[4:])] or {}).get("videoUrl"): p["video"] = c["reels"][int(k[4:])]["videoUrl"]
            elif k.startswith("reel") or k in ("linkedin", "instagram"):
                if v.get("driveUrl"): p["video"] = (v.get("longVideos") or {}).get(k) or v["driveUrl"]
            pieces.append(p)
        item = {"id": vid, "order": v.get("order", 0), "title": f"Video {v.get('order')}: {v.get('title','')}", "videoUrl": v.get("driveUrl", ""),
                "note": v.get("amyNote", ""), "sentAt": now, "pieces": pieces}
        if v.get("posterId"):
            f = enc(v["posterId"], f"{vid}-poster")
            if f: item["poster"] = f
        items[vid] = item
        u = next((u for u in updates if u["doc_id"] == vid), None)
        d = {"amyState": "sent", "amySentAt": now}
        if u: u["data"].update(d)
        else: updates.append({"doc_id": vid, "version": rec["version"], "data": d})
    internal["items"] = sorted(items.values(), key=lambda x: x.get("order", 0)); internal["updatedAt"] = now
    json.dump(encrypt(acode, json.dumps(internal).encode()), open("internal.enc.json", "w"))
    # Amy -> studio
    edits = [x for x in tabs.get("Edits", []) if x.get("videoId")]
    done = {x.get("videoId"): x for x in tabs.get("AmyStatus", []) if x.get("videoId") and x.get("done_at")}
    for vid, rec in videos.items():
        mine = [x for x in edits if x.get("videoId") == vid]
        if not mine and vid not in done: continue
        ae = {}; appr = {}; notes = {}
        sent_at = rec["data"].get("amySentAt") or 0
        for x in mine:
            if x.get("field") == "__note":
                if num(x.get("at")) > sent_at:
                    notes[x.get("pieceKey", "")] = {"key": x.get("pieceKey", ""), "text": x.get("text", ""), "by": x.get("name", ""), "at": num(x.get("at"))}
                continue
            if x.get("field") == "__approved":
                if num(x.get("at")) > sent_at:
                    appr[x.get("pieceKey", "")] = {"yes": x.get("text") == "yes", "by": x.get("name", ""), "at": num(x.get("at"))}
                continue
            try: path = json.loads(x.get("path") or "[]")
            except Exception: path = []
            if not path:  # the sheet export can lose the path; rebuild it from the post and field label
                try:
                    path = next((f["path"] for f in amy_fields(rec["data"].get("content") or {}, x.get("pieceKey", "")) if f["label"] == x.get("field")), [])
                except Exception: path = []
            ae.setdefault(x.get("pieceKey", ""), []).append({"label": x.get("field", ""), "path": path, "text": x.get("text", ""), "note": x.get("note", ""), "by": x.get("name", ""), "at": num(x.get("at"))})
        d = {"amyEdits": ae, "amyApproved": [{"key": k, "by": a["by"], "at": a["at"]} for k, a in appr.items() if a["yes"]], "amyNotes": [n for n in notes.values() if n["text"].strip()]}
        v = rec["data"]
        if vid in done and v.get("amyState") == "sent" and num(done[vid].get("done_at")) > (v.get("amySentAt") or 0):
            dn = done[vid]; d.update({"amyState": "back", "amyBackAt": num(dn.get("done_at")), "amyBackNote": dn.get("note", "")})
            ps = dict(v.get("pieceStatus") or {})
            for k in ps:
                if ps[k] in ("Draft", "With Amy"): ps[k] = "Amy approved" if (appr.get(k) or {}).get("yes") else "Amy edits back"
            d["pieceStatus"] = ps; d["status"] = "Ready for AARP"
            # Amy sent it back: her copy becomes the copy (the old copy is kept for Undo).
            old = v.get("content") or {}; newc = json.loads(json.dumps(old)); n_applied = 0
            orig = rec.get("orig_content") or old
            for k_, lst in ae.items():
                for e in sorted(lst, key=lambda e: e["at"]):
                    if e["at"] <= sent_at: continue  # only this round's edits
                    pth = e.get("path") or []
                    if not pth: continue
                    o = newc
                    try:
                        for seg in pth[:-1]: o = o[seg]
                        if o[pth[-1]] != e["text"]:
                            o[pth[-1]] = e["text"]; n_applied += 1
                    except Exception: pass
            if n_applied:
                d["prevContent"] = orig; d["content"] = newc; d["amyAppliedAt"] = now; d["amyAppliedCount"] = n_applied
        u = next((u for u in updates if u["doc_id"] == vid), None)
        if u: u["data"].update(d)
        else: updates.append({"doc_id": vid, "version": rec["version"], "data": d})

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--passcode", required=True); ap.add_argument("--videos", required=True)
    ap.add_argument("--sheet", required=True); ap.add_argument("--assets", default="")
    ap.add_argument("--out", required=True); ap.add_argument("--amy-passcode", default="")
    a = ap.parse_args()
    code = a.passcode
    content = json.loads(decrypt(code, json.load(open("content.enc.json"))))
    items = {it["id"]: it for it in content.get("items", [])}

    videos = {}
    for f in glob.glob(os.path.join(a.videos, "**", "*.json"), recursive=True):
        d = json.load(open(f)); vid = d.get("id") or os.path.splitext(os.path.basename(f))[0]
        videos[vid] = {"data": d.get("data", d), "version": d.get("version")}
        _v = videos[vid]["data"]; _cf = _v.get("clipFiles") or {}
        for _r in ((_v.get("content") or {}).get("reels") or []):
            if isinstance(_r, dict) and not _r.get("videoUrl") and _cf.get(_r.get("opusClipId")): _r["videoUrl"] = _cf[_r["opusClipId"]]

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
    try: early_tabs = parse_sheet(open(a.sheet).read())
    except Exception: early_tabs = {}
    for vid, d in amy_promote(videos, early_tabs, now).items():
        updates.append({"doc_id": vid, "version": videos[vid]["version"], "data": d})
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
        ps = v.get("pieceStatus") or {}
        keys = [k for k in piece_keys(c, g) if ps.get(k) != "Skip"]
        # Only posts Amy approved go to AARP, unless ETC chose "Send to AARP (skip Amy)" (status "Sent to AARP").
        # Posts AARP is already reviewing stay. "Pulled back" posts never go. No fallback to unapproved drafts.
        amy_ok = {a.get("key") for a in (v.get("amyApproved") or []) if isinstance(a, dict)}
        def eligible(k):
            s_ = ps.get(k)
            if s_ in ("Pulled back", "Skip"): return False
            if s_ == "Sent to AARP": return True  # ETC chose to bypass Amy for this post
            if s_ in ("Reviewer approved", "Deb approved") and k in (v.get("clientSnap") or {}): return True  # already on AARP's site
            return k in amy_ok and s_ not in ("Draft", "With Amy")  # Amy approved it on her Edit Desk
        keys = [k for k in keys if eligible(k)]
        if not keys:
            if vid in items: items[vid]["hidden"] = True
            removed.append(vid)
            updates.append({"doc_id": vid, "version": rec["version"], "data": {"clientState": "removed", "clientSyncedAt": now}})
            continue
        gi = v.get("graphicsImages") or []
        def img_for(k):
            if k not in ("stat", "stat2", "quote", "carousel"): return []
            out = []
            for n, im in enumerate(x for x in gi if x.get("piece") == k):
                f = enc_image(im["id"], f"{vid}-{k}-{n+1}")
                if f: out.append({"file": f, "alt": {"stat": "Stat graphic", "stat2": "Stat graphic", "quote": "Quote card", "carousel": "Carousel slide"}.get(k, "Graphic")})
                else: missing.append(im["id"])
            return out
        def poster_for(aid, name):
            f = enc_image(aid, name)
            if not f: missing.append(aid)
            return f
        item = {"id": vid, "order": v.get("order", 0), "title": f"Video {v.get('order')}: {v.get('title','')}" if v.get("kind") == "finished" else v.get("title", ""),
                "topic": v.get("topic", ""), "videoUrl": v.get("driveUrl", ""), "duration": full_len(v), "graphicsUrl": "", "sentAt": now,
                "note": v.get("clientNote", ""), "pieces": [build_piece({**v, "_id": vid}, k, img_for, poster_for) for k in keys]}
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
    phos = [x for x in tabs.get("Photos", []) if x.get("id") and not x.get("removed")]
    for vid, it in items.items():
        if vid not in videos or it.get("hidden"): continue
        cs, pstat = {}, {}
        for p in it.get("pieces", []):
            r = dec.get(f"{vid}--{p['key']}", {})
            entry = {"label": p.get("label", "")}
            if r.get("reviewer_status"): entry["reviewer"] = {"status": r["reviewer_status"], "note": r.get("reviewer_note", ""), "by": r.get("reviewer_name", ""), "at": num(r.get("reviewer_at"))}
            if r.get("deb_status"): entry["deb"] = {"status": r["deb_status"], "note": r.get("deb_note", ""), "by": r.get("deb_name", ""), "at": num(r.get("deb_at"))}
            entry["comments"] = [{"by": x.get("name", ""), "text": x.get("text", ""), "at": num(x.get("at"))} for x in cmts if x.get("videoId") == vid and x.get("pieceKey") == p["key"]]
            ph = [{"by": x.get("name", ""), "at": num(x.get("at")), "note": x.get("note", ""), "url": x.get("fileUrl", "")} for x in phos if x.get("videoId") == vid and x.get("pieceKey") == p["key"]]
            if ph: entry["photos"] = ph
            cs[p["key"]] = entry
            if entry.get("deb", {}).get("status") == "approved": pstat[p["key"]] = "Deb approved"
            elif entry.get("reviewer", {}).get("status") == "approved": pstat[p["key"]] = "Reviewer approved"
        data = {"clientStatus": cs, "clientSyncedAt": now}
        if pstat: data["pieceStatus"] = pstat
        if cs and all(e.get("deb", {}).get("status") == "approved" for e in cs.values()): data["status"] = "Approved"
        u = next((u for u in updates if u["doc_id"] == vid), None)
        if u: u["data"].update(data)
        else: updates.append({"doc_id": vid, "version": videos[vid]["version"], "data": data})

    if a.amy_passcode:
        amy_sync(a.amy_passcode, videos, tabs, updates, asset_file, now)

    merged = {}
    for u in updates:  # one entry per studio doc
        if u["doc_id"] in merged: merged[u["doc_id"]]["data"].update(u["data"])
        else: merged[u["doc_id"]] = {"doc_id": u["doc_id"], "version": u["version"], "data": dict(u["data"])}
    updates = list(merged.values())
    json.dump(updates, open(a.out, "w"), indent=1)
    print(json.dumps({"sent": sent, "removed": removed, "studio_updates": len(updates), "missing_images": missing}))

if __name__ == "__main__":
    main()
