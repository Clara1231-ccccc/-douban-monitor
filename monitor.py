
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
    # RSSHub 的豆瓣用户动态 JSON 接口
    url = f"https://rsshub.app/douban/user/{DOUBAN_USER_ID}/status"
    # 也尝试不带 /status 的路由
    url2 = f"https://rsshub.app/douban/user/{DOUBAN_USER_ID}"

    for feed_url in [url, url2]:
        try:
            print(f"📡 尝试 RSSHub: {feed_url}")
            resp = requests.get(feed_url, headers=HEADERS, timeout=30)

            if resp.status_code != 200:
                print(f"   ↳ 状态码 {resp.status_code}，跳过")
                continue

            # RSSHub 返回的是 RSS XML，用 feedparser 解析
            import feedparser

            feed = feedparser.parse(resp.content)
            if not feed.entries:
                print(f"   ↳ 无条目，跳过")
                continue

            print(f"✅ RSSHub 成功，获取到 {len(feed.entries)} 条动态")
            result = []
            for entry in feed.entries:
                # 取 HTML 描述并转纯文本
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
    """方法二：直接爬取豆瓣页面（需要登录 Cookie，更可靠）"""
    if not DOUBAN_COOKIE:
        print("⚠️  未设置豆瓣 Cookie，跳过直接爬取")
        return None

    url = f"https://www.douban.com/people/{DOUBAN_USER_ID}/statuses"
    headers = {**HEADERS, "Cookie": DOUBAN_COOKIE}

    try:
        print(f"📡 直接爬取: {url}")
        resp = requests.get(url, headers=headers, timeout=30)

        if resp.status_code != 200:
            print(f"   ↳ 豆瓣返回状态码 {resp.status_code}")
            return None

        # 用正则提取动态 ID 和内容（比 BeautifulSoup 轻量，不需要额外安装）
        # 豆瓣动态页每个条目通常有类似 data-status-id="xxxx" 的属性
        ids = re.findall(r'data-status-id=["\'](\d+)["\']', resp.text)
        contents = re.findall(
            r'class="status-content[^"]*"[^>]*>([\s\S]*?)</div>', resp.text
        )

        if not ids:
            # 尝试另一种匹配模式
            ids = re.findall(r'/status/(\d+)/', resp.text)
            # 去重并保留顺序
            seen = set()
            ids = [x for x in ids if not (x in seen or seen.add(x))]

        if not ids:
            print("⚠️  解析页面未找到动态，可能是页面结构变了或 Cookie 失效")
            return None

        # 取最新的 20 条
        ids = ids[:20]
        result = []
        for i, sid in enumerate(ids):
            content = contents[i] if i < len(contents) else ""
            result.append(
                {
                    "id": sid,
                    "title": f"豆瓣动态",
                    "content": strip_html(content),
                    "link": f"https://www.douban.com/people/{DOUBAN_USER_ID}/status/{sid}/",
                    "published": "",
                }
            )

        # 去重（同一个页面可能有多个地方出现同一个 ID）
        seen = set()
        unique_result = []
        for item in result:
            if item["id"] not in seen:
                seen.add(item["id"])
                unique_result.append(item)

        print(f"✅ 直接爬取成功，获取到 {len(unique_result)} 条动态")
        return unique_result

    except Exception as e:
        print(f"⚠️  直接爬取失败: {e}")
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

