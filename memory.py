#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
学员记忆
=======
这个平台的「长期记忆」全部在这一层。模型本身没有记忆(见术语「记忆」),所以「知道你在哪儿卡壳」
这件事,是这里把每一次对话产生的信号存下来、聚合成画像、再在下一次讲解时塞回提示词。

记什么(两层,和术语「长期记忆的设计」里讲的一致):
  事实层  每个术语的状态(没学 / 在学 / 懂了 / 卡住)、测验对错、
          困难类型计数、有效讲法计数  —— 数量少,每次讲解都用
  情节层  每轮对话模型给出的信号 + 一句备注,定期让模型归纳成一段「学员画像」—— 数量多,只取近期

困难类型和讲法类型是固定的小词表,不让模型自由发挥 —— 自由发挥的标签没法聚合,
两个月后画像就是一堆互相不认识的词。

存 SQLite,单文件,在 data/ 下。删掉就是一个全新学员。
"""
import json, os, sqlite3, threading, time, hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "learn.db")

# 困难类型:模型每轮从这里面挑,不能自造
DIFF = {
    "abstract": "抽象概念落不了地",
    "tech":     "技术细节看不懂",
    "english":  "英文术语记不住",
    "confuse":  "和相近概念分不清",
    "why":      "不明白为什么需要它",
    "scale":    "数量级 / 成本没概念",
    "flow":     "流程顺序理不清",
}
# 讲法类型:哪种解释方式让学员「懂了」
STYLE = {
    "analogy":  "生活比喻",
    "example":  "产品场景例子",
    "step":     "一步一步拆开",
    "contrast": "和相近概念对比",
    "concrete": "看具体的数据 / 格式",
    "short":    "先给结论再展开",
}
STATUSES = ("new", "learning", "got", "confused")
DECAY_DAYS = 30          # 信号按时间衰减,一个月前的只算一半

_lock = threading.Lock()


def _conn():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS progress(
            term TEXT PRIMARY KEY, status TEXT DEFAULT 'new',
            opened INT DEFAULT 0, confused INT DEFAULT 0,
            quiz_right INT DEFAULT 0, quiz_wrong INT DEFAULT 0,
            first_seen REAL, last_seen REAL, got_at REAL);
        CREATE TABLE IF NOT EXISTS chat(
            id INTEGER PRIMARY KEY, term TEXT, role TEXT, content TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS signal(
            id INTEGER PRIMARY KEY, term TEXT, ts REAL,
            difficulty TEXT, style TEXT, understood TEXT, note TEXT);
        CREATE TABLE IF NOT EXISTS learner(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS supplement(
            term TEXT, phash TEXT, title TEXT, text TEXT, ts REAL,
            PRIMARY KEY(term, phash));
        CREATE INDEX IF NOT EXISTS chat_term ON chat(term, id);
        CREATE INDEX IF NOT EXISTS signal_ts ON signal(ts);
        """)


# ---------- 事实层:进度 ----------
def touch(term):
    """打开一个术语。第一次打开从 new 变 learning。"""
    now = time.time()
    with _lock, _conn() as c:
        r = c.execute("SELECT status FROM progress WHERE term=?", (term,)).fetchone()
        if not r:
            c.execute("INSERT INTO progress(term,status,opened,first_seen,last_seen) VALUES(?,?,1,?,?)",
                      (term, "learning", now, now))
        else:
            c.execute("UPDATE progress SET opened=opened+1, last_seen=? WHERE term=?", (now, term))


def set_status(term, status):
    if status not in STATUSES: raise ValueError("状态不对")
    now = time.time()
    with _lock, _conn() as c:
        c.execute("INSERT OR IGNORE INTO progress(term,status,first_seen,last_seen) VALUES(?,?,?,?)",
                  (term, status, now, now))
        if status == "got":
            c.execute("UPDATE progress SET status='got', got_at=?, last_seen=? WHERE term=?", (now, now, term))
        elif status == "confused":
            c.execute("UPDATE progress SET status='confused', confused=confused+1, last_seen=? WHERE term=?", (now, term))
        else:
            c.execute("UPDATE progress SET status=?, last_seen=? WHERE term=?", (status, now, term))


def quiz(term, correct):
    now = time.time()
    with _lock, _conn() as c:
        c.execute("INSERT OR IGNORE INTO progress(term,status,first_seen,last_seen) VALUES(?,?,?,?)",
                  (term, "learning", now, now))
        col = "quiz_right" if correct else "quiz_wrong"
        c.execute(f"UPDATE progress SET {col}={col}+1, last_seen=? WHERE term=?", (now, term))


def progress_all():
    with _lock, _conn() as c:
        return {r["term"]: dict(r) for r in c.execute("SELECT * FROM progress")}


def progress(term):
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM progress WHERE term=?", (term,)).fetchone()
        return dict(r) if r else {"term": term, "status": "new", "opened": 0, "confused": 0,
                                  "quiz_right": 0, "quiz_wrong": 0}


# ---------- 情节层:对话与信号 ----------
def add_chat(term, role, content):
    with _lock, _conn() as c:
        c.execute("INSERT INTO chat(term,role,content,ts) VALUES(?,?,?,?)", (term, role, content, time.time()))


def chats(term, limit=40):
    with _lock, _conn() as c:
        rows = c.execute("SELECT role,content,ts FROM chat WHERE term=? ORDER BY id DESC LIMIT ?",
                         (term, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def clear_chat(term):
    with _lock, _conn() as c:
        c.execute("DELETE FROM chat WHERE term=?", (term,))


def add_signal(term, sig):
    """sig = {difficulty:[...], style_worked:[...], understood:'yes|partly|no', note:str}。词表外的标签丢掉。"""
    d = [x for x in (sig.get("difficulty") or []) if x in DIFF]
    s = [x for x in (sig.get("style_worked") or []) if x in STYLE]
    u = sig.get("understood") if sig.get("understood") in ("yes", "partly", "no") else "partly"
    with _lock, _conn() as c:
        c.execute("INSERT INTO signal(term,ts,difficulty,style,understood,note) VALUES(?,?,?,?,?,?)",
                  (term, time.time(), json.dumps(d), json.dumps(s), u, (sig.get("note") or "")[:200]))
        c.execute("UPDATE learner SET value=CAST(CAST(value AS INT)+1 AS TEXT) WHERE key='since_summary'")
        if c.execute("SELECT 1 FROM learner WHERE key='since_summary'").fetchone() is None:
            c.execute("INSERT INTO learner VALUES('since_summary','1')")
    return {"difficulty": d, "style_worked": s, "understood": u}


def recent_signals(n=30):
    with _lock, _conn() as c:
        rows = c.execute("SELECT * FROM signal ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    out = []
    for r in reversed(rows):
        out.append({"term": r["term"], "ts": r["ts"], "difficulty": json.loads(r["difficulty"]),
                    "style": json.loads(r["style"]), "understood": r["understood"], "note": r["note"]})
    return out


def learner_get(key, default=""):
    with _lock, _conn() as c:
        r = c.execute("SELECT value FROM learner WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def learner_set(key, value):
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO learner VALUES(?,?)", (key, str(value)))


# ---------- 画像:把事实和情节聚合成一段能塞进提示词的话 ----------
def profile(terms=None):
    """terms 是术语表(list of dict),用来算「弱在哪一类」。"""
    now = time.time()
    dcount = {k: 0.0 for k in DIFF}
    scount = {k: 0.0 for k in STYLE}
    n_sig = 0
    with _lock, _conn() as c:
        for r in c.execute("SELECT ts,difficulty,style FROM signal"):
            w = 1.0 if now - r["ts"] < DECAY_DAYS * 86400 else 0.5
            n_sig += 1
            for k in json.loads(r["difficulty"]): dcount[k] += w
            for k in json.loads(r["style"]): scount[k] += w
    prog = progress_all()
    got = [t for t, p in prog.items() if p["status"] == "got"]
    confused = [t for t, p in prog.items() if p["status"] == "confused"]
    qr = sum(p["quiz_right"] for p in prog.values()); qw = sum(p["quiz_wrong"] for p in prog.values())

    # 弱类别:卡住 + 答错 的术语按类别聚合
    weak_cat = {}
    if terms:
        by = {t["id"]: t for t in terms}
        for tid, p in prog.items():
            if tid not in by: continue
            bad = (2 if p["status"] == "confused" else 0) + p["quiz_wrong"] + p["confused"]
            if bad: weak_cat[by[tid]["cat"]] = weak_cat.get(by[tid]["cat"], 0) + bad

    top_d = [k for k, v in sorted(dcount.items(), key=lambda kv: -kv[1]) if v > 0][:3]
    top_s = [k for k, v in sorted(scount.items(), key=lambda kv: -kv[1]) if v > 0][:2]
    return {
        "signals": n_sig, "got": len(got), "confused": len(confused), "learning": sum(1 for p in prog.values() if p["status"] == "learning"),
        "quiz_right": qr, "quiz_wrong": qw,
        "difficulty": [{"key": k, "label": DIFF[k], "score": round(dcount[k], 1)} for k in sorted(dcount, key=lambda k: -dcount[k])],
        "style": [{"key": k, "label": STYLE[k], "score": round(scount[k], 1)} for k in sorted(scount, key=lambda k: -scount[k])],
        "top_difficulty": top_d, "top_style": top_s,
        "weak_cat": sorted(weak_cat.items(), key=lambda kv: -kv[1])[:3],
        "memory": learner_get("memory"), "memory_at": learner_get("memory_at"),
        "since_summary": int(learner_get("since_summary", "0") or 0),
    }


def profile_text(p):
    """给提示词用的一段话。没信号时返回空串,提示词里就不提。"""
    if not p["signals"] and not p["memory"] and not p["got"]:
        return ""
    lines = []
    if p["memory"]: lines.append("学员画像:" + p["memory"])
    if p["top_difficulty"]: lines.append("常见困难:" + "、".join(DIFF[k] for k in p["top_difficulty"]))
    if p["top_style"]: lines.append("对他有效的讲法:" + "、".join(STYLE[k] for k in p["top_style"]))
    if p["weak_cat"]: lines.append("薄弱的类别:" + "、".join(c for c, _ in p["weak_cat"]))
    lines.append(f"已掌握 {p['got']} 个术语,卡住 {p['confused']} 个,测验 {p['quiz_right']} 对 {p['quiz_wrong']} 错")
    return "\n".join(lines)


def profile_hash(p):
    """画像的指纹。补充讲解按 (术语, 指纹) 缓存 —— 画像没变就不重新生成。"""
    key = "|".join(p["top_difficulty"]) + "#" + "|".join(p["top_style"]) + "#" + (p["memory"] or "")[:80]
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:10]


# ---------- 补充讲解缓存 ----------
def get_supplement(term, phash):
    with _lock, _conn() as c:
        r = c.execute("SELECT title,text,ts FROM supplement WHERE term=? AND phash=?", (term, phash)).fetchone()
    return dict(r) if r else None


def save_supplement(term, phash, title, text):
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO supplement VALUES(?,?,?,?,?)", (term, phash, title, text, time.time()))


def latest_supplement(term):
    with _lock, _conn() as c:
        r = c.execute("SELECT title,text,ts,phash FROM supplement WHERE term=? ORDER BY ts DESC LIMIT 1", (term,)).fetchone()
    return dict(r) if r else None


# ---------- 自适应:下一个学什么 ----------
def recommend(terms, n=5):
    """规则:
       1. 卡住的先回头(权重最高)
       2. 前置都懂了的才推荐;前置没学的排后面并标出来
       3. 薄弱类别加权 —— 卡在哪儿多练哪儿
       4. 和最近掌握的术语相关的加一点权,顺着学
       5. 同等条件下先低级别"""
    prog = progress_all()
    p = profile(terms)
    weak = {c for c, _ in p["weak_cat"]}
    recent_got = sorted([(v["got_at"] or 0, t) for t, v in prog.items() if v["status"] == "got"], reverse=True)[:3]
    recent_got = {t for _, t in recent_got}
    got = {t for t, v in prog.items() if v["status"] == "got"}
    out = []
    for t in terms:
        st = prog.get(t["id"], {}).get("status", "new")
        if st == "got": continue
        missing = [r for r in t["prereq"] if r not in got]
        score = 0; why = []
        if st == "confused": score += 100; why.append("上次卡住了,回头再看一眼")
        if not missing: score += 20
        else: score -= 10 * len(missing)
        if t["cat"] in weak: score += 8; why.append(f"「{t['cat']}」这一类你练得还不够")
        if any(r in recent_got for r in t["related"] + t["prereq"]): score += 5; why.append("和你刚学会的内容相连")
        score -= t["level"] * 3
        if st == "learning": score += 2; why.append("看过但还没标「懂了」")
        if not why: why.append("按顺序该到它了")
        out.append({"id": t["id"], "name": t["name"], "level": t["level"], "cat": t["cat"],
                    "score": score, "why": why[0], "missing": missing, "status": st})
    out.sort(key=lambda x: (-x["score"], x["level"]))
    return out[:n]


def reset():
    with _lock:
        if os.path.exists(DB): os.remove(DB)
    init()
