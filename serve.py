#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 产品经理入门 · 服务
=====================
标准库 HTTP,只监听 127.0.0.1。多线程是因为一次模型调用要几秒到几十秒。

GET  /                       页面
GET  /api/terms              全部术语 + 每条的学习状态 + 推荐的下一步
GET  /api/term?id=           一条术语:正文、进度、对话记录、最近一次补充讲解
GET  /api/profile            学员画像 + 推荐 + 近期信号
GET  /api/settings
POST /api/open       {id}                     记一次打开
POST /api/status     {id, status}             懂了 / 卡住 / 重学
POST /api/quiz       {id, choice}             自测作答
POST /api/chat       {id, message}            追问。回答 + 信号进记忆;攒够信号就顺手归纳一次画像
POST /api/chat/clear {id}
POST /api/supplement {id, force?}             按画像再讲一遍(画像没变走缓存)
POST /api/reset      {}                       清空学员记忆(术语表不动)
POST /api/settings / /api/settings/test       模型供应商
"""
import argparse, json, os, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import provider, memory, tutor

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8440
SUMMARY_EVERY = 6           # 每攒 6 条信号归纳一次画像

TERMS = json.load(open(os.path.join(HERE, "terms.json"), encoding="utf-8"))
BY_ID = {t["id"]: t for t in TERMS}
LEVELS = {1: "入门:先把词认全", 2: "进阶:智能体怎么干活", 3: "深入:产品经理的取舍"}


def _one(q, k, default=None):
    v = q.get(k); return v[0] if v else default


def _term(d):
    t = BY_ID.get(d.get("id") or "")
    if not t: raise ValueError("没有这个术语")
    return t


def _ai_ready():
    c = provider.load()
    return bool(c.get("api_key")) or c["kind"] == "openai_compat" and "127.0.0.1" in (c.get("base_url") or "")


def api_terms(q):
    prog = memory.progress_all()
    out = []
    for t in TERMS:
        p = prog.get(t["id"])
        out.append({k: t[k] for k in ("id", "name", "en", "level", "cat", "one", "prereq")} |
                   {"status": p["status"] if p else "new"})
    prof = memory.profile(TERMS)
    return {"terms": out, "levels": LEVELS, "next": memory.recommend(TERMS, 4),
            "profile": {k: prof[k] for k in ("got", "confused", "learning", "signals")},
            "ai_ready": _ai_ready()}


def api_term(q):
    t = BY_ID.get(_one(q, "id") or "")
    if not t: raise ValueError("没有这个术语")
    prog = memory.progress_all()
    rel = [{"id": r, "name": BY_ID[r]["name"], "status": prog.get(r, {}).get("status", "new")} for r in t["related"] if r in BY_ID]
    pre = [{"id": r, "name": BY_ID[r]["name"], "status": prog.get(r, {}).get("status", "new")} for r in t["prereq"] if r in BY_ID]
    return {"term": t, "progress": memory.progress(t["id"]), "chats": memory.chats(t["id"]),
            "supplement": memory.latest_supplement(t["id"]), "related": rel, "prereq": pre,
            "ai_ready": _ai_ready()}


def api_profile(q):
    prof = memory.profile(TERMS)
    sig = memory.recent_signals(12)
    for s in sig:
        s["term_name"] = BY_ID.get(s["term"], {}).get("name", s["term"])
        s["difficulty_label"] = [memory.DIFF[k] for k in s["difficulty"]]
        s["style_label"] = [memory.STYLE[k] for k in s["style"]]
    prog = memory.progress_all()
    return {"profile": prof, "next": memory.recommend(TERMS, 6), "signals": sig,
            "got": [{"id": t, "name": BY_ID[t]["name"]} for t, p in prog.items() if p["status"] == "got" and t in BY_ID],
            "confused": [{"id": t, "name": BY_ID[t]["name"]} for t, p in prog.items() if p["status"] == "confused" and t in BY_ID],
            "total": len(TERMS), "ai_ready": _ai_ready()}


def post_open(d):
    t = _term(d); memory.touch(t["id"]); return {"ok": True}


def post_status(d):
    t = _term(d); memory.set_status(t["id"], d.get("status"))
    return {"ok": True, "progress": memory.progress(t["id"]), "next": memory.recommend(TERMS, 4)}


def post_quiz(d):
    t = _term(d)
    try: ch = int(d.get("choice"))
    except Exception: raise ValueError("要选一个")
    ok = ch == t["quiz"]["answer"]
    memory.quiz(t["id"], ok)
    if not ok:
        # 答错本身就是一个信号:记一条「和相近概念分不清 / 抽象」的弱信号,让画像能感知测验
        memory.add_signal(t["id"], {"difficulty": ["confuse"], "style_worked": [], "understood": "partly",
                                    "note": f"自测选错:选了「{t['quiz']['options'][ch][:20]}」"})
    return {"ok": True, "correct": ok, "answer": t["quiz"]["answer"], "why": t["quiz"]["why"],
            "progress": memory.progress(t["id"])}


def _maybe_summarize(prof):
    if prof["since_summary"] < SUMMARY_EVERY: return None
    try:
        new, i, o = tutor.summarize(prof["memory"], memory.recent_signals(20), prof)
        memory.learner_set("memory", new); memory.learner_set("memory_at", str(int(__import__("time").time())))
        memory.learner_set("since_summary", "0")
        return new
    except Exception:
        return None


def post_chat(d):
    t = _term(d)
    msg = (d.get("message") or "").strip()
    if not msg: raise ValueError("先说点什么")
    if len(msg) > 2000: raise ValueError("一次别超过 2000 字")
    history = memory.chats(t["id"])
    prof = memory.profile(TERMS)
    reply, sig, i, o = tutor.chat(t, history, msg, prof)
    memory.add_chat(t["id"], "user", msg)
    memory.add_chat(t["id"], "assistant", reply)
    sig = memory.add_signal(t["id"], sig)
    if sig["understood"] == "no": memory.set_status(t["id"], "confused")
    elif sig["understood"] == "partly" and memory.progress(t["id"])["status"] == "new": memory.set_status(t["id"], "learning")
    prof = memory.profile(TERMS)
    updated = _maybe_summarize(prof)
    return {"ok": True, "reply": reply, "signals": {**sig, "difficulty_label": [memory.DIFF[k] for k in sig["difficulty"]],
                                                     "style_label": [memory.STYLE[k] for k in sig["style_worked"]]},
            "tokens": [i, o], "memory_updated": updated, "progress": memory.progress(t["id"])}


def post_chat_clear(d):
    t = _term(d); memory.clear_chat(t["id"]); return {"ok": True}


def post_supplement(d):
    t = _term(d)
    prof = memory.profile(TERMS)
    h = memory.profile_hash(prof)
    if not d.get("force"):
        c = memory.get_supplement(t["id"], h)
        if c: return {"ok": True, "cached": True, **c}
    title, text, i, o = tutor.supplement(t, prof)
    memory.save_supplement(t["id"], h, title, text)
    return {"ok": True, "cached": False, "title": title, "text": text, "tokens": [i, o]}


def post_reset(d):
    memory.reset(); return {"ok": True}


def settings_test(d):
    try:
        cfg = provider.load(); r = provider.ping(cfg)
        return {"ok": True, "msg": f"通了 · {cfg['model']} · 结构化输出模式 {r['mode']}"}
    except Exception as e:
        t = str(e)
        if "401" in t or "authentication" in t.lower() or "invalid_api_key" in t: hint = "key 不对或已失效"
        elif "403" in t: hint = "key 没有这个模型的权限"
        elif "404" in t: hint = "模型名或 base_url 不对"
        elif "429" in t: hint = "被限流了"
        elif "Errno" in t or "URLError" in t or "连不上" in t: hint = "连不上,检查 base_url 和网络"
        else: hint = t[:160]
        return {"ok": False, "msg": "没通 —— " + hint}


GETS = {"/api/terms": api_terms, "/api/term": api_term, "/api/profile": api_profile,
        "/api/settings": lambda q: provider.public()}
POSTS = {"/api/open": post_open, "/api/status": post_status, "/api/quiz": post_quiz,
         "/api/chat": post_chat, "/api/chat/clear": post_chat_clear, "/api/supplement": post_supplement,
         "/api/reset": post_reset, "/api/settings/test": settings_test,
         "/api/settings": lambda d: (provider.save(d), {"ok": True, "settings": provider.public(), "msg": "已保存"})[1]}


class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            b = open(os.path.join(HERE, "app.html"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(b); return
        if u.path not in GETS:
            self.send_response(404); self.end_headers(); return
        try: self._json(GETS[u.path](parse_qs(u.query)))
        except ValueError as e: self._json({"ok": False, "msg": str(e)}, 400)
        except Exception as e: self._json({"ok": False, "msg": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path not in POSTS:
            self.send_response(404); self.end_headers(); return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n) or b"{}")
            out, code = POSTS[u.path](d), 200
        except ValueError as e: out, code = {"ok": False, "msg": str(e)}, 400
        except provider.LLMError as e: out, code = {"ok": False, "msg": str(e)}, 502
        except Exception as e: out, code = {"ok": False, "msg": f"{type(e).__name__}: {e}"}, 500
        self._json(out, code)

    def log_message(self, *a): pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="AI 产品经理入门")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    memory.init()
    try: srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    except OSError: raise SystemExit(f"端口 {a.port} 已经被占着。先停掉:  kill $(lsof -ti:{a.port})")
    print(f"已启动 → http://127.0.0.1:{a.port}   ({len(TERMS)} 个术语;Ctrl+C 停止)")
    if not a.no_browser and not os.environ.get("BROWSER") == "true":
        try: webbrowser.open(f"http://127.0.0.1:{a.port}")
        except Exception: pass
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\n停了。")


if __name__ == "__main__":
    main()
