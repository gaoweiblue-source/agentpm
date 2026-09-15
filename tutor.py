#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导师
====
三种调用,全部走 provider.complete(),全部要结构化输出:

  chat        学员在某个术语下追问。回答之外,模型还要交一份「信号」:这一轮学员卡在什么类型的困难上、
              哪种讲法起了作用、懂了没有。信号进 memory,回答进对话记录。
  supplement  按学员画像给某个术语再讲一遍。画像没变就走缓存。
  summarize   每攒够 N 条信号,把旧画像 + 近期信号归纳成一段新的画像(≤120 字)。

提示词的三条原则,和平台定位一致:
  - 说人话。默认对方是零基础的产品经理,不是工程师。术语第一次出现要解释。
  - 短。一轮回答 200 字以内,除非学员要求展开。
  - 一次只用一个比喻。比喻多了反而把人绕晕。
"""
import json
import provider, memory

SIG_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "signals": {
            "type": "object",
            "properties": {
                "difficulty": {"type": "array", "items": {"type": "string", "enum": list(memory.DIFF)}},
                "style_worked": {"type": "array", "items": {"type": "string", "enum": list(memory.STYLE)}},
                "understood": {"type": "string", "enum": ["yes", "partly", "no"]},
                "note": {"type": "string"},
            },
            "required": ["difficulty", "style_worked", "understood", "note"],
            "additionalProperties": False,
        },
    },
    "required": ["reply", "signals"], "additionalProperties": False,
}
SIG_EXAMPLE = json.dumps({"reply": "……", "signals": {"difficulty": ["abstract"], "style_worked": ["analogy"],
                                                      "understood": "partly", "note": "把 token 和字混在一起了"}},
                         ensure_ascii=False)

SUP_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}, "text": {"type": "string"}},
              "required": ["title", "text"], "additionalProperties": False}
SUM_SCHEMA = {"type": "object", "properties": {"memory": {"type": "string"}},
              "required": ["memory"], "additionalProperties": False}

PERSONA = """你是一位教「AI / 大模型 / 智能体」的导师,学员是零基础、想入门 AI 产品经理的人。
规则:
- 说人话。不用没解释过的术语;英文缩写第一次出现时给中文。
- 短。一次回答 200 字以内,除非学员明确要求展开。先给结论,再解释。
- 一次最多用一个比喻。比喻要贴近日常生活或产品工作。
- 不要说「作为 AI」之类的话,不要客套开头,直接回答。
- 学员说错了要温和指出,不要顺着错的说。
- 回答末尾可以追加一个很短的确认问题(不超过 20 字),帮学员检查自己是否真懂了;不是每次都要。"""


def _term_block(t):
    return (f"当前术语:{t['name']}({t['en']})\n一句话:{t['one']}\n"
            f"标准讲解:{' '.join(t['explain'])}\n比喻:{t['analogy']}\n产品例子:{t['example']}\n常见误解:{t['pitfall']}")


def chat(term, history, message, prof):
    ptxt = memory.profile_text(prof)
    system = PERSONA + "\n\n" + _term_block(term)
    if ptxt:
        system += ("\n\n这个学员的长期记忆(据此调整讲法,但不要向学员复述这段):\n" + ptxt)
    system += ("\n\n输出 JSON,两个字段:\n"
               "reply:给学员的回答(字符串里不要用英文双引号,引用请用「」)。\n"
               "signals:你对这一轮的观察,四个子字段 ——\n"
               "  difficulty:这一轮暴露出的困难类型,只能从下面的 key 里选,没有就空数组:\n" +
               "".join(f"    {k} = {v}\n" for k, v in memory.DIFF.items()) +
               "  style_worked:你判断这一轮里哪种讲法对他起了作用,只能从下面的 key 里选,没把握就空数组:\n" +
               "".join(f"    {k} = {v}\n" for k, v in memory.STYLE.items()) +
               "  understood:学员目前对这个术语的理解程度,yes / partly / no\n"
               "  note:一句不超过 40 字的备注,写具体卡点")
    convo = "\n".join(f"{'学员' if h['role'] == 'user' else '导师'}:{h['content']}" for h in history[-12:])
    user = (f"之前的对话:\n{convo}\n\n" if convo else "") + f"学员刚说:{message}"
    d, i, o = provider.complete(system, user, SIG_SCHEMA, max_tokens=1500, example=SIG_EXAMPLE)
    reply = (d.get("reply") or "").strip()
    sig = d.get("signals") or {}
    if not isinstance(sig, dict): sig = {}
    return reply, sig, i, o


def supplement(term, prof):
    ptxt = memory.profile_text(prof)
    system = PERSONA + "\n\n" + _term_block(term)
    system += ("\n\n任务:学员已经看过上面的标准讲解,但根据他的长期记忆,标准讲解可能不对他的路子。"
               "请针对他的困难类型、用对他有效的讲法,把这个术语**换一种方式**再讲一遍。"
               "不要重复标准讲解里已有的比喻和例子。250 字以内,可以用 Markdown 的加粗和短列表。"
               "\n\n学员的长期记忆:\n" + (ptxt or "(还没有信号,按零基础讲)") +
               "\n\n输出 JSON:title 是这段补充讲解的小标题(10 字以内,点出用了什么讲法),text 是正文。")
    d, i, o = provider.complete(system, "开始。", SUP_SCHEMA, max_tokens=1500,
                                example=json.dumps({"title": "用一个报销流程来讲", "text": "……"}, ensure_ascii=False))
    return (d.get("title") or "换个讲法").strip(), (d.get("text") or "").strip(), i, o


def summarize(old, signals, prof):
    lines = []
    for s in signals:
        lines.append(f"[{s['term']}] 困难={','.join(memory.DIFF[k] for k in s['difficulty']) or '无'} "
                     f"有效讲法={','.join(memory.STYLE[k] for k in s['style']) or '无'} "
                     f"理解={s['understood']} 备注={s['note']}")
    system = ("你在维护一份学员画像,给后续的 AI 导师看。要求:第三人称,120 字以内,只写对「下次怎么讲」有用的事:"
              "他在哪类概念上反复卡、哪种讲法对他管用、有没有明显的误解模式、进步在哪。"
              "旧画像里仍然成立的要保留,被新证据推翻的要更新。不要写客套话,不要列数字。输出 JSON:memory。")
    user = (f"旧画像:{old or '(空)'}\n\n统计:" + memory.profile_text(dict(prof, memory="")) +
            "\n\n近期信号:\n" + "\n".join(lines))
    d, i, o = provider.complete(system, user, SUM_SCHEMA, max_tokens=600,
                                example=json.dumps({"memory": "……"}, ensure_ascii=False))
    return (d.get("memory") or old or "").strip(), i, o
