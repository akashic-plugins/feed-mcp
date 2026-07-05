#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["requests"]
# ///
"""
RSSHub Route Finder
输入任意网页 URL，自动匹配本地 RSSHub 路由并生成订阅链接。
"""

import sys
import json
import re
from urllib.parse import urlparse

import requests

RSSHUB_BASE = "http://localhost:1200"

def fetch_json(path):
    """获取本地 RSSHub API 数据"""
    try:
        resp = requests.get(f"{RSSHUB_BASE}{path}", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        print(f"❌ 无法连接本地 RSSHub ({RSSHUB_BASE}): {e}")
        print("💡 请确认 Docker 容器是否运行: docker ps | grep rsshub")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"❌ RSSHub 返回了非 JSON 数据，可能 API 路径变更或服务异常。")
        sys.exit(1)

def extract_domain(url):
    """从 URL 提取主域名 (例如: www.github.com -> github.com)"""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    # 去除 www. 前缀
    if domain.startswith("www."):
        domain = domain[4:]
    return domain

def match_rule(url, rules):
    """
    匹配雷达规则。
    rules 结构: {"github.com": {"_name": "GitHub", ".": [{"title": "...", "source": [...], "target": "..."}]}}
    注意：规则分散在 namespace 下的各个子 key 中（如 ".", "ds", "tf" 等），需遍历收集。
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip('/')
    domain = extract_domain(url)

    matched_rules = []
    
    for namespace, ns_data in rules.items():
        # 跳过元数据
        if namespace.startswith("_"):
            continue
            
        # 1. 匹配域名
        if namespace != domain and f"www.{namespace}" != domain:
            continue
        
        # 2. 收集该 namespace 下所有子模块的规则
        all_rules = []
        if isinstance(ns_data, dict):
            for sub_key, sub_data in ns_data.items():
                if sub_key.startswith("_"):  # 跳过 "_name"
                    continue
                if isinstance(sub_data, list):
                    all_rules.extend(sub_data)
        elif isinstance(ns_data, list):
            all_rules = ns_data
        
        # 3. 遍历规则匹配路径
        for rule in all_rules:
            if not isinstance(rule, dict):
                continue
                
            sources = rule.get("source", [])
            target = rule.get("target", "")
            docs = rule.get("docs", "")
            
            if not target:
                continue

            for source_pattern in sources:
                # 标准化 pattern: 确保以 / 开头
                if not source_pattern.startswith("/"):
                    source_pattern = "/" + source_pattern
                    
                # 构建正则
                regex_parts = []
                param_names = []
                
                # 分割路径 (跳过开头的空字符串)
                pattern_segments = source_pattern.split('/')[1:]
                
                for segment in pattern_segments:
                    if segment.startswith(":"):
                        param_names.append(segment[1:])
                        regex_parts.append(r"([^/]+)")
                    elif segment == "*":
                        regex_parts.append(r".*")
                    else:
                        regex_parts.append(re.escape(segment))
                
                if not regex_parts:
                    # 如果 pattern 只是 "/"，则只匹配根路径
                    regex_str = "^/$"
                else:
                    # 注意：segments 已经去掉了前导空字符串，所以这里要补回 '/'
                    regex_str = "^/" + "/".join(regex_parts) + "$"
                
                match = re.match(regex_str, path)
                
                if match:
                    # 匹配成功！提取参数
                    params = dict(zip(param_names, match.groups()))
                    
                    # 构建最终路由
                    final_route = target
                    for key, value in params.items():
                        final_route = final_route.replace(f":{key}", value)
                    
                    matched_rules.append({
                        "namespace": namespace,
                        "route": final_route,
                        "docs": docs,
                        "params": params
                    })
    
    return matched_rules

def main():
    if len(sys.argv) < 2:
        print("用法: python find_route.py <URL>")
        print("示例: python find_route.py https://github.com/DIYgod/RSSHub")
        sys.exit(1)

    url = sys.argv[1]
    print(f"🔍 正在分析 URL: {url}")
    print("-" * 40)

    # 1. 获取雷达规则
    # 注意：RSSHub 的 /api/radar/rules 返回的是所有规则的集合
    rules = fetch_json("/api/radar/rules")
    
    # 2. 匹配
    matches = match_rule(url, rules)

    if not matches:
        print("❌ 未找到匹配的 RSSHub 路由。")
        print("💡 可能原因:")
        print("   1. 该网站尚未被 RSSHub 支持。")
        print("   2. URL 路径不符合已知规则 (尝试去掉尾部参数)。")
        print("   3. 本地 RSSHub 版本过旧，请更新镜像。")
        
        # 兜底：尝试搜索 namespace
        print("\n🔎 建议手动查找:")
        print(f"   访问 {RSSHUB_BASE}/api/namespace 查看所有支持的网站列表。")
        sys.exit(0)

    print(f"✅ 找到 {len(matches)} 个匹配的路由:\n")

    for i, m in enumerate(matches, 1):
        full_url = f"{RSSHUB_BASE}{m['route']}"
        print(f"[{i}] 来源: {m['namespace']}")
        print(f"    文档: {m['docs']}")
        print(f"    路由: {m['route']}")
        print(f"    🔗 订阅链接: {full_url}")
        
        # 检查是否有缺失参数 (虽然匹配到了，但有些参数可能是可选的或在 query 中)
        # 这里简化处理，假设匹配到的都是完整的
        
        print("-" * 40)

    print("💡 使用方法:")
    print(f"   将上面的链接添加到你的 RSS 阅读器，或对 bot 说: '订阅 {matches[0]['route']}'")

if __name__ == "__main__":
    main()
