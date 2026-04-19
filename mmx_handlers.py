"""MiniMax mmx tool handlers for handcraft-mcp."""

def rmmx(a, t=120):
    import subprocess as s, shutil
    mmx_bin = shutil.which("mmx") or "mmx"
    # Windows npm .cmd files require shell=True to execute correctly
    return s.run([mmx_bin] + a, capture_output=True, text=True, timeout=t, shell=True)

def hmi(r, a):
    p = a.get("prompt", "").strip()
    if not p:
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": "Error: prompt required"}], "isError": True}}
    args = ["image", "generate", "--prompt", p, "--output", "json", "--quiet"]
    for k, v in [("aspect_ratio", "--aspect-ratio"), ("n", "--n"), ("out_dir", "--out-dir")]:
        if a.get(k):
            args.append(v)
            args.append(str(a[k]))
    try:
        x = rmmx(args)
        if x.returncode == 0:
            txt = "Image(s) generated:\n" + x.stdout.strip()
            is_err = False
        else:
            txt = "mmx error: " + x.stderr
            is_err = True
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": txt}], "isError": is_err}}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": "Error: " + str(e)}], "isError": True}}

def hmvd(r, a):
    p = a.get("prompt", "").strip()
    if not p:
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": "Error: prompt required"}], "isError": True}}
    args = ["video", "generate", "--prompt", p, "--output", "json", "--quiet"]
    if a.get("async"):
        args.append("--async")
    for k, v in [("first_frame", "--first-frame"), ("download", "--download")]:
        if a.get(k):
            args.append(v)
            args.append(a[k])
    try:
        x = rmmx(args, t=300)
        if x.returncode == 0:
            txt = "Video generation:\n" + x.stdout.strip()
            is_err = False
        else:
            txt = "mmx error: " + x.stderr
            is_err = True
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": txt}], "isError": is_err}}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": "Error: " + str(e)}], "isError": True}}

def hms(r, a):
    t = a.get("text", "").strip()
    tf = a.get("text_file", "").strip()
    if not t and not tf:
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": "Error: text or text_file required"}], "isError": True}}
    args = ["speech", "synthesize", "--output", "json", "--quiet"]
    if t:
        args.append("--text")
        args.append(t)
    if tf:
        args.append("--text-file")
        args.append(tf)
    for k, v in [("voice", "--voice"), ("model", "--model"), ("speed", "--speed"), ("format", "--format"), ("out", "--out")]:
        if a.get(k):
            args.append(v)
            args.append(str(a[k]))
    try:
        x = rmmx(args)
        if x.returncode == 0:
            txt = "Speech synthesized:\n" + x.stdout.strip()
            is_err = False
        else:
            txt = "mmx error: " + x.stderr
            is_err = True
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": txt}], "isError": is_err}}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": "Error: " + str(e)}], "isError": True}}

def hmu(r, a):
    p = a.get("prompt", "").strip()
    l = a.get("lyrics", "").strip()
    if not p and not l:
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": "Error: prompt or lyrics required"}], "isError": True}}
    args = ["music", "generate", "--output", "json", "--quiet"]
    if p:
        args.append("--prompt")
        args.append(p)
    if l:
        args.append("--lyrics")
        args.append(l)
    for k, v in [("vocals", "--vocals"), ("genre", "--genre"), ("mood", "--mood"), ("instruments", "--instruments"), ("out", "--out")]:
        if a.get(k):
            args.append(v)
            args.append(a[k])
    if a.get("bpm"):
        args.append("--bpm")
        args.append(str(a["bpm"]))
    if a.get("instrumental"):
        args.append("--instrumental")
    try:
        x = rmmx(args, t=180)
        if x.returncode == 0:
            txt = "Music generated:\n" + x.stdout.strip()
            is_err = False
        else:
            txt = "mmx error: " + x.stderr
            is_err = True
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": txt}], "isError": is_err}}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": "Error: " + str(e)}], "isError": True}}

def hmv(r, a):
    img = a.get("image", "").strip()
    fid = a.get("file_id", "").strip()
    if not img and not fid:
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": "Error: image or file_id required"}], "isError": True}}
    args = ["vision", "describe", "--output", "json"]
    if img:
        args.append("--image")
        args.append(img)
    if fid:
        args.append("--file-id")
        args.append(fid)
    if a.get("prompt"):
        args.append("--prompt")
        args.append(a["prompt"])
    try:
        x = rmmx(args)
        if x.returncode == 0:
            txt = x.stdout.strip()
        else:
            txt = "mmx error: " + x.stderr
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": txt}], "isError": x.returncode != 0}}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": "Error: " + str(e)}], "isError": True}}

def hmsq(r, a):
    q = a.get("q", "").strip()
    if not q:
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": "Error: q required"}], "isError": True}}
    try:
        x = rmmx(["search", "query", "--q", q, "--output", "json", "--quiet"])
        if x.returncode == 0:
            txt = x.stdout.strip()
        else:
            txt = "mmx error: " + x.stderr
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": txt}], "isError": x.returncode != 0}}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": "Error: " + str(e)}], "isError": True}}

def hmc(r, a):
    msg = a.get("message", "").strip()
    if not msg:
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": "Error: message required"}], "isError": True}}
    args = ["text", "chat", "--message", msg, "--output", "json", "--quiet"]
    if a.get("system"):
        args.append("--system")
        args.append(a["system"])
    if a.get("model"):
        args.append("--model")
        args.append(a["model"])
    if a.get("max_tokens"):
        args.append("--max-tokens")
        args.append(str(a["max_tokens"]))
    if a.get("temperature"):
        args.append("--temperature")
        args.append(str(a["temperature"]))
    try:
        x = rmmx(args)
        if x.returncode == 0:
            txt = x.stdout.strip()
        else:
            txt = "mmx error: " + x.stderr
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": txt}], "isError": x.returncode != 0}}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": "Error: " + str(e)}], "isError": True}}

def hmq(r, a):
    try:
        x = rmmx(["quota", "show", "--output", "json", "--quiet"])
        if x.returncode == 0:
            txt = x.stdout.strip()
        else:
            txt = "mmx error: " + x.stderr
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": txt}], "isError": x.returncode != 0}}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": r, "result": {"content": [{"type": "text", "text": "Error: " + str(e)}], "isError": True}}

DISPATCH = {
    "mmx_image_generate": hmi,
    "mmx_video_generate": hmvd,
    "mmx_speech_synthesize": hms,
    "mmx_music_generate": hmu,
    "mmx_vision_describe": hmv,
    "mmx_search_query": hmsq,
    "mmx_text_chat": hmc,
    "mmx_quota_show": hmq,
}
