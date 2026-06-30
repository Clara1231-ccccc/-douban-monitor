#!/usr/bin/env python3
"""
豆瓣用户动态监控器
自动检查指定豆瓣用户的最新动态，通过 Server酱 推送到微信
运行在 GitHub Actions 上，无需自建服务器
"""

import os
import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests

# ==================== 配置 ====================
DOUBAN_USER_ID = os.environ.get("DOUBAN_USER_ID", "")
SERVERCHAN_SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "")
DOUBAN_COOKIE = os.environ.get("DOUBAN_COOKIE", "")

STATE_FILE = Path("state.json")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}


# ==================== 工具函数 ====================

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_id": ""}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:200]


# ==================== 获取豆瓣动态 ====================

def fetch_via_rsshub():
    """方法一：通过 RSSHub 获取（无需 Cookie）"""
    # RSSHub 的豆瓣用户动态路由，多个镜像轮流尝试
    bases = [
        "https://rsshub.app",
        "https://rsshub.sku.moe",
        "https://rsshub.bili.xyz",
    ]

    for base in bases:
        for path in [f"/douban/user/{DOUBAN_USER_ID}/status", f"/douban/user/{DOUBAN_USER_ID}"]:
            url = f"{base}{path}"
            try:
                print(f"📡 尝试 RSSHub: {url}")
                resp = requests.get(url, headers=HEADERS, timeout=30)

                if resp.status_code != 200:
                    print(f"   ↳ 状态码 {resp.status_code}，跳过")
                    continue

                import feedparser
                feed = feedparser.parse(resp.content)
                if not feed.entries:
                    print(f"   ↳ 无条目，跳过")
                    continue

                print(f"✅ RSSHub 成功，获取到 {len(feed.entries)} 条动态")
                result = []
                for entry in feed.entries:
                    desc = entry.get("summary", "") or entry.get("description", "")
                    result.append(
                        {
                            "id": entry.get("id", "") or entry.get("link", ""),
                            "title": entry.get("title", "豆瓣动态"),
                            "content": strip_html(desc),
                            "link": entry.get("link", ""),
                            "published": entry.get("published", ""),
                        }
                    )
                return result

            except Exception as e:
                print(f"   ↳ 出错: {e}")
                continue

    return None


def fetch_via_scraping():
    """方法二：直接爬取豆瓣页面（需要登录 Cookie）"""
    if not DOUBAN_COOKIE:
        print("⚠️  未设置豆瓣 Cookie，跳过直接爬取")
        return None

    headers = {**HEADERS, "Cookie": DOUBAN_COOKIE}
    results = []

    # 豆瓣动态是通过 API 异步加载的，模拟其请求
    api_url = "https://www.douban.com/j/people/{}/statuses"
    api_urls = [
        api_url.format(DOUBAN_USER_ID),
        f"https://www.douban.com/people/{DOUBAN_USER_ID}/statuses?p=1",
    ]

    for url in api_urls:
        try:
            print(f"📡 尝试: {url}")
            resp = requests.get(url, headers=headers, timeout=30)
            print(f"   ↳ 状态码: {resp.status_code}, 长度: {len(resp.text)}")

            if resp.status_code != 200:
                continue

            # 尝试解析 JSON（豆瓣 j/ 路径可能返回 JSON）
            try:
                data = resp.json()
                if isinstance(data, dict) and "items" in data:
                    items = data["items"]
                elif isinstance(data, list):
                    items = data
                else:
                    items = []

                for item in items:
                    sid = str(item.get("id", ""))
                    content = strip_html(item.get("text", item.get("content", "")))
                    if sid:
                        results.append({
                            "id": sid,
                            "title": "豆瓣动态",
                            "content": content,
                            "link": f"https://www.douban.com/people/{DOUBAN_USER_ID}/status/{sid}/",
                        })
                if results:
                    print(f"✅ API JSON 解析成功，获取到 {len(results)} 条动态")
                    return results
            except (json.JSONDecodeError, TypeError):
                pass

            # 如果不是 JSON，尝试从 HTML 中找动态
            ids = re.findall(r'/status/(\d+)/', resp.text)
            seen = set()
            ids = [x for x in ids if not (x in seen or seen.add(x))]
            if ids:
                ids = ids[:20]
                for sid in ids:
                    results.append({
                        "id": sid,
                        "title": "豆瓣动态",
                        "content": "",
                        "link": f"https://www.douban.com/people/{DOUBAN_USER_ID}/status/{sid}/",
                    })
                print(f"✅ HTML 解析成功，获取到 {len(results)} 条动态")
                return results

        except Exception as e:
            print(f"   ↳ 出错: {e}")
            continue

    # 最终方案：在页面中搜索 API 地址和 JSON 数据
    try:
        desktop_url = f"https://www.douban.com/people/{DOUBAN_USER_ID}/statuses"
        resp = requests.get(desktop_url, headers=headers, timeout=30)
        html = resp.text

        # 找所有 script 标签里的内容
        scripts = re.findall(r'<script[^>]*>([\s\S]*?)</script>', html)
        for i, script in enumerate(scripts):
            if 'status' in script.lower() or 'api' in script.lower() or 'url' in script.lower():
                print(f"   ↳ script[{i}] 包含关键词片段:\n{script[:500]}\n---")
                break
        else:
            # 没找到，打印页面中可能包含数据的部分
            idx = html.find('status') or html.find('statuses') or html.find('status_list')
            if idx > 0:
                print(f"   ↳ 找到 status 关键词位置，附近内容:\n{html[max(0,idx-100):idx+300]}")
            else:
                print(f"   ↳ 未找到 status 关键词，尝试搜 id 或 list...")
                for kw in ['"list"', '"items"', '"data"', '"content"', 'window.__']:
                    idx = html.find(kw)
                    if idx > 0:
                        print(f"   ↳ 找到 '{kw}' 在位置 {idx}:\n{html[idx:idx+300]}")
                        break
    except Exception as e:
        print(f"   ↳ 搜索出错: {e}")

    print(f"⚠️  所有方式都解析失败")
    return None


def fetch_statuses():
    """综合获取——先试 RSSHub，不行就试直接爬取"""
    result = fetch_via_rsshub()
    if result is not None:
        return result
    print("📡 RSSHub 不可用，尝试直接爬取...")
    result = fetch_via_scraping()
    if result is not None:
        return result
    print("❌ 所有获取方式都失败了")
    return []


# ==================== 微信推送 ====================

def push_to_wechat(items):
    """通过 Server酱 推送到微信"""
    for item in items:
        title = item.get("title", "豆瓣新动态")
        content = item.get("content", "")
        link = item.get("link", "")

        # 构建 Markdown 消息体（desp）
        desp = content
        if link:
            desp += f"\n\n[👉 在豆瓣中查看]({link})"

        try:
            resp = requests.get(
                f"https://sctapi.ftqq.com/{SERVERCHAN_SENDKEY}.send",
                params={"title": title, "desp": desp},
                timeout=15,
            )
            result = resp.json()
            if result.get("code") == 0:
                print(f"✅ 推送成功: {title[:30]}")
            else:
                print(f"❌ 推送失败: {result}")
        except Exception as e:
            print(f"❌ 推送异常: {e}")

        time.sleep(1)  # 每条之间间隔 1 秒，防限流


# ==================== 主函数 ====================

def main():
    print("=" * 50)
    print(f"🔍 豆瓣动态监控")
    print(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"👤 用户 ID: {DOUBAN_USER_ID}")
    print("=" * 50)

    state = load_state()
    print(f"📝 上次最后动态 ID: {state.get('last_id', '无')}")

    entries = fetch_statuses()
    if not entries:
        print("⚠️  未获取到任何动态")
        print("   可能原因：")
        print("   • 用户不存在或已注销")
        print("   • 该用户近期无新动态")
        print("   • 网络问题（GitHub Actions 在美国，可能被豆瓣限制）")
        print("   • 建议设置豆瓣 Cookie（见教程）")
        return

    # 找到新动态（最新条目排前面，遇到已推送的就停）
    new_entries = []
    for entry in entries:
        if entry["id"] == state.get("last_id"):
            break
        new_entries.append(entry)

    if not new_entries:
        print("💤 没有新动态，无需推送")
        return

    # RSSHub 最新在前，反转成最早的在前面（按时间顺序推送）
    new_entries.reverse()
    print(f"🎉 发现 {len(new_entries)} 条新动态，开始推送...")

    push_to_wechat(new_entries)

    # 更新状态：记录最新一条的 ID
    state["last_id"] = new_entries[-1]["id"]
    save_state(state)
    print(f"✅ 完成！最新动态 ID: {state['last_id']}")


if __name__ == "__main__":
    main()
