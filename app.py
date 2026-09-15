#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 产品经理入门 · 学习平台
========================
一条命令启动:

    python3 app.py

打开 http://127.0.0.1:8440。左边是学习路径,右边是术语讲解。术语表本身不需要模型,
「继续问」「按我的方式再讲一遍」「学员画像归纳」三样需要 —— 到设置里选供应商、填 key。
自己目录下没有 config.json 时,回退读上一级目录(选题台)的。
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

if __name__ == "__main__":
    import provider
    c = provider.load()
    if not c.get("api_key"): print("还没设 API key。术语表能看,追问和自适应讲解要到「设置」里填 key。\n")
    elif c.get("inherited"): print(f"沿用选题台的设置:{c['preset']} · {c['model']}\n")
    import serve
    serve.main(sys.argv[1:])
