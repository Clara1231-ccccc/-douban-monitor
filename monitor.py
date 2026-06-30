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

    import feedparser

    # 尝试多个 URL 和解析方式
    urls = [
        f"https://www.douban.com/people/{DOUBAN_USER_ID}/statuses",
        f"https://m.douban.com/people/{DOUBAN_USER_ID}/statuses",
    ]

    for url in urls:
        headers = {**HEADERS, "Cookie": DOUBAN_COOKIE}
        try:
            print(f"📡 直接爬取: {url}")
            resp = requests.get(url, headers=headers, timeout=30)

            print(f"   ↳ 状态码: {resp.status_code}, 响应长度: {len(resp.text)}")
            if resp.status_code != 200:
                print(f"   ↳ 跳过")
                continue

            # 检查是否返回了登录页面
            if "登录" in resp.text[:500] or "注册" in resp.text[:500]:
                print("⚠️  返回了登录页，Cookie 可能无效或过期")
                continue

            # 方法 1：匹配 data-status-id
            ids = re.findall(r'data-status-id=["\'](\d+)["\']', resp.text)
            if ids:
                print(f"   ↳ 方式1匹配到 {len(ids)} 个动态")
                break

            # 方法 2：匹配 /status/数字/
            ids = re.findall(r'/status/(\d+)/', resp.text)
            seen = set()
            ids = [x for x in ids if not (x in seen or seen.add(x))]
            if ids:
                print(f"   ↳ 方式2匹配到 {len(ids)} 个动态")
                break

            # 方法 3：匹配 status_id 变量
            ids = re.findall(r'"id":\s*(\d+)', resp.text)
            if ids:
                print(f"   ↳ 方式3匹配到 {len(ids)} 个动态")
                break

        except Exception as e:
            print(f"   ↳ 爬取出错: {e}")
            continue

    # 所有 URL 都试过了
    if not ids:
        print(f"⚠️  所有方式都未解析到动态，页面前200字: {resp.text[:200]}")
        return None

    # 去重取前20
    seen = set()
    ids = [x for x in ids if not (x in seen or seen.add(x))][:20]

    # 提取内容（如果有）
    ptn = r'class="status-content[^"]*"[^>]*>([\s\S]*?)</div>'
    contents = re.findall(ptn, resp.text)

    result = []
    for i, sid in enumerate(ids):
        content = contents[i] if i < len(contents) else ""
        result.append({
            "id": sid,
            "title": "豆瓣动态",
            "content": strip_html(content),
            "link": f"https://www.douban.com/people/{DOUBAN_USER_ID}/status/{sid}/",
            "published": "",
        })

    print(f"✅ 直接爬取成功，获取到 {len(result)} 条动态")
    return result


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
