#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
课程工作台 · 模型供应商
======================
从「炒饭会选题台」原样搬来的一层,改动只有一处:自己的 config.json 不存在时,
回退读上一级目录(选题台)的 config.json,这样不用把 key 填两遍。

设置的存放,和「一次带结构化输出的调用」这件事在不同厂商上的统一写法。

两条路:
  anthropic       走官方 SDK,用 output_config.format 做结构化输出
  openai_compat   走 OpenAI 兼容协议(DeepSeek、Kimi、智谱、通义、OpenRouter、
                  本地 Ollama/LM Studio 都是这个协议),stdlib 发 HTTP,不加依赖

结构化输出在三方那边支持得参差不齐,所以是三级降级:
  json_schema  →  json_object  →  纯提示词要求 + 容错解析
一次调用里降级成功会记住,后面不再重试上一级。

密钥存 config.json,权限 0600,**永远不回传给浏览器**(前端只拿到打码版)。
本服务只监听 127.0.0.1。
"""
import json, os, re, ssl, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "config.json")
PARENT_CONF = os.path.join(os.path.dirname(HERE), "config.json")   # 选题台的设置,回退用

# 预置。base_url 是稳定的,模型名各家会变 —— 界面上可以改,以你的服务商文档为准。
PRESETS = [
    {"id": "anthropic", "name": "Anthropic", "kind": "anthropic",
     "base_url": "", "model": "claude-opus-5",
     "note": "官方 SDK。结构化输出最稳,案例抽取的默认选择。"},
    {"id": "deepseek", "name": "DeepSeek", "kind": "openai_compat",
     "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
     "note": "OpenAI 兼容。便宜,中文好。"},
    {"id": "moonshot", "name": "月之暗面 Kimi", "kind": "openai_compat",
     "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-32k",
     "note": "OpenAI 兼容。长上下文。"},
    {"id": "zhipu", "name": "智谱 GLM", "kind": "openai_compat",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-plus",
     "note": "OpenAI 兼容。"},
    {"id": "qwen", "name": "阿里通义千问", "kind": "openai_compat",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-max",
     "note": "OpenAI 兼容模式。"},
    {"id": "openrouter", "name": "OpenRouter", "kind": "openai_compat",
     "base_url": "https://openrouter.ai/api/v1", "model": "anthropic/claude-opus-4.1",
     "note": "一个 key 转发到各家。"},
    {"id": "ollama", "name": "本地 Ollama / LM Studio", "kind": "openai_compat",
     "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:32b",
     "note": "本地跑,不花钱也不出网。质量取决于你的模型。"},
    {"id": "custom", "name": "自定义(OpenAI 兼容)", "kind": "openai_compat",
     "base_url": "", "model": "",
     "note": "任何提供 /chat/completions 的服务。"},
]

# 每百万 token 的美元价。三方留 0 表示不估价 —— 与其编一个错的数字,不如不显示。
PRICE = {
    "claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0), "claude-opus-4-8": (5.0, 25.0),
}

DEFAULT = {"preset": "anthropic", "kind": "anthropic", "base_url": "",
           "model": "claude-opus-5", "api_key": "", "effort": "medium", "workers": 4}


# ---------- 设置的读写 ----------
def load():
    c = dict(DEFAULT)
    src = CONF if os.path.exists(CONF) else (PARENT_CONF if os.path.exists(PARENT_CONF) else None)
    if src:
        try: c.update(json.load(open(src, encoding="utf-8")))
        except Exception: pass
        c["inherited"] = (src == PARENT_CONF)
    if not c.get("api_key"):                      # 环境变量兜底
        c["api_key"] = os.environ.get("ANTHROPIC_API_KEY", "") if c["kind"] == "anthropic" else ""
        c["from_env"] = bool(c["api_key"])
    return c


def save(patch):
    c = dict(DEFAULT)
    src = CONF if os.path.exists(CONF) else (PARENT_CONF if os.path.exists(PARENT_CONF) else None)
    if src:
        try: c.update(json.load(open(src, encoding="utf-8")))
        except Exception: pass
    for k, v in patch.items():
        if k == "api_key" and v in ("", None, MASK_KEEP):
            continue                              # 空或占位符 = 不动原来的密钥
        if k in ("preset", "kind", "base_url", "model", "api_key", "effort", "workers"):
            c[k] = v
    fd = os.open(CONF, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)
    os.chmod(CONF, 0o600)
    return c


MASK_KEEP = "••••••••"


def mask(k):
    if not k: return ""
    return k[:6] + "…" + k[-4:] if len(k) > 14 else "•" * len(k)


def public():
    """给浏览器看的设置 —— 密钥只出现打码版,原文永远不出这台机器的这个进程。"""
    c = load()
    return {"preset": c["preset"], "kind": c["kind"], "base_url": c["base_url"],
            "model": c["model"], "effort": c["effort"], "workers": c["workers"],
            "key_set": bool(c["api_key"]), "key_hint": mask(c["api_key"]),
            "from_env": c.get("from_env", False), "inherited": c.get("inherited", False),
            "presets": PRESETS,
            "priced": c["model"] in PRICE}


def price(model=None):
    return PRICE.get(model or load()["model"], (0.0, 0.0))


# ---------- 一次调用 ----------
class LLMError(RuntimeError):
    pass


_DEGRADE = {}          # {base_url: 'json_object' | 'plain'} 降级记忆


def _json_from_text(t):
    """三方经常裹一层 ```json 或者前后带话。容错解析。"""
    t = t.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", t, re.S)
    if m: t = m.group(1).strip()
    try: return json.loads(t)
    except json.JSONDecodeError: pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try: return json.loads(t[i:j + 1])
        except json.JSONDecodeError:
            try: return json.loads(_repair_quotes(t[i:j + 1]))
            except json.JSONDecodeError as e:
                raise LLMError(f"模型返回的 JSON 解析不了({e.msg}),再发一次通常就好")
    raise LLMError(f"返回的不是 JSON:{t[:180]}")


def _repair_quotes(t):
    """三方模型偶尔在字符串值里放没转义的英文双引号(「说"做完了"」)。
    走一遍状态机:字符串内部遇到 " 时,看它后面(跳过空白)是不是 , } ] : —— 是就当结束,不是就转义。"""
    out, ins, i, n = [], False, 0, len(t)
    while i < n:
        c = t[i]
        if not ins:
            out.append(c)
            if c == '"': ins = True
        else:
            if c == "\\":
                out.append(c); i += 1
                if i < n: out.append(t[i])
            elif c == '"':
                j = i + 1
                while j < n and t[j] in " \t\r\n": j += 1
                if j >= n or t[j] in ",}]:":
                    out.append(c); ins = False
                else:
                    out.append('\\"')
            else:
                out.append(c)
        i += 1
    return "".join(out)


def _anthropic(cfg, system, user, schema, effort, max_tokens):
    try:
        import anthropic
    except ImportError:
        raise LLMError("缺 SDK:pip3 install anthropic")
    kw = {"api_key": cfg["api_key"]} if cfg["api_key"] else {}
    cl = anthropic.Anthropic(**kw)
    oc = {"format": {"type": "json_schema", "schema": schema}}
    if effort and not _DEGRADE.get("anthropic_effort"):
        oc["effort"] = effort
    try:
        r = cl.messages.create(
            model=cfg["model"], max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            thinking={"type": "adaptive"}, output_config=oc)
    except Exception as ex:
        # effort 和 format 能不能同放一个 output_config,文档里没有例子。被拒就去掉 effort。
        if "effort" in oc and ("output_config" in str(ex) or "effort" in str(ex)):
            _DEGRADE["anthropic_effort"] = True
            oc.pop("effort")
            r = cl.messages.create(
                model=cfg["model"], max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                thinking={"type": "adaptive"}, output_config=oc)
        else:
            raise LLMError(f"{type(ex).__name__}: {ex}")
    if r.stop_reason == "refusal":
        raise LLMError(f"模型拒答:{getattr(r.stop_details, 'category', None)}")
    txt = "".join(b.text for b in r.content if b.type == "text")
    return _json_from_text(txt), r.usage.input_tokens, r.usage.output_tokens


def _post(url, key, payload, timeout=600):
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"}, method="POST")
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode())


def _is_reasoner(model):
    m = (model or "").lower()
    return any(k in m for k in ("reasoner", "-r1", "thinking", "o1", "o3"))


def _openai_compat(cfg, system, user, schema, effort, max_tokens, example=None):
    base = (cfg["base_url"] or "").rstrip("/")
    if not base: raise LLMError("没填 base_url")
    url = base + "/chat/completions"
    key = base + "|" + (cfg.get("model") or "")
    # 推理模型不支持结构化输出,直接走纯文本;否则每试一级都是一次两分钟的完整推理
    mode = _DEGRADE.get(key, "plain" if _is_reasoner(cfg.get("model")) else "json_schema")
    # 降级模式下**给样例,不给 schema**。把 schema 序列化进提示词,模型会把 schema 原样吐回来
    # (踩过:23 讲全部返回那段 schema 文本,每次都恰好 459 字符)。
    hint = ("\n\n只输出一个 JSON 对象,不要解释、不要 markdown 代码块、"
            "不要把这段格式说明本身抄回来。格式就照这个样子:\n"
            + (example or json.dumps(schema, ensure_ascii=False)))
    for attempt in ("json_schema", "json_object", "plain"):
        if ("json_schema", "json_object", "plain").index(attempt) < \
           ("json_schema", "json_object", "plain").index(mode):
            continue
        body = {"model": cfg["model"],
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user if attempt != "plain" else user + hint}],
                "max_tokens": max_tokens, "temperature": 0}
        if _is_reasoner(cfg.get("model")):
            # 推理模型把思考过程也算进 max_tokens,给小了最终答案就是空的。放大到至少 3 万。
            body["max_tokens"] = min(max(max_tokens * 4, 32000), 64000)
            body.pop("temperature", None)
        if attempt == "json_schema":
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "out", "strict": True, "schema": schema}}
        elif attempt == "json_object":
            body["messages"][0]["content"] = system + hint
            body["response_format"] = {"type": "json_object"}
        try:
            d = _post(url, cfg["api_key"], body)
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            if e.code in (400, 422) and attempt != "plain":
                _DEGRADE[key] = ("json_object" if attempt == "json_schema" else "plain")
                continue                      # 这家不支持这一级,降一级重来
            raise LLMError(f"HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            raise LLMError(f"连不上 {base}:{e.reason}")
        ch = (d.get("choices") or [{}])[0]
        txt = (ch.get("message") or {}).get("content") or ""
        u = d.get("usage") or {}
        if (ch.get("finish_reason") == "length") and not txt.strip():
            raise LLMError("模型输出被截断(思考过程用完了 token 上限),最终答案为空。换 deepseek-chat,或者减少一次生成的内容量。")
        if not txt.strip() and attempt != "plain":
            # 非推理模型偶尔也会空正文,当作这一级不支持,降一级重来
            _DEGRADE[key] = ("json_object" if attempt == "json_schema" else "plain")
            continue
        _DEGRADE[key] = attempt
        out = _json_from_text(txt)
        if isinstance(out, dict) and "properties" in out and set(out) <= {
                "type", "properties", "required", "additionalProperties", "$schema"}:
            raise LLMError("模型把 schema 抄回来了,没有产出数据")
        return (out, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
    raise LLMError("三级都没成功")


def complete(system, user, schema, effort=None, max_tokens=8000, cfg=None, example=None):
    """统一入口。返回 (解析好的 dict, in_tok, out_tok)。

    example 是给降级模式用的一行样例输出 —— 三方模型跟样例走,不跟 schema 走。"""
    cfg = cfg or load()
    if not cfg.get("api_key") and cfg["kind"] != "openai_compat":
        raise LLMError("还没设 API key。到「设置」里填。")
    if cfg["kind"] == "anthropic":
        return _anthropic(cfg, system, user, schema, effort, max_tokens)
    return _openai_compat(cfg, system, user, schema, effort, max_tokens, example)


def ping(cfg=None):
    """连通性自检:一次极小的调用。"""
    cfg = cfg or load()
    sch = {"type": "object", "properties": {"ok": {"type": "boolean"}},
           "required": ["ok"], "additionalProperties": False}
    d, i, o = complete("你是一个只回 JSON 的接口。", "返回 {\"ok\": true}", sch,
                       effort="low", max_tokens=200, cfg=cfg)
    return {"ok": bool(d.get("ok")), "in": i, "out": o,
            "mode": _DEGRADE.get((cfg.get("base_url") or "").rstrip("/") + "|" + (cfg.get("model") or ""), "json_schema")
                    if cfg["kind"] == "openai_compat" else "anthropic"}
