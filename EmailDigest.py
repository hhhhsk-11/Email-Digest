#!/usr/bin/env python
# coding: utf-8

# In[8]:


import imaplib
import email
import json
import requests
from email.header import decode_header
from datetime import datetime, timedelta
import socket
socket.setdefaulttimeout(30)

# ========== 配置区（只改这里）==========
QQ_EMAIL = ""
QQ_AUTH_CODE = ""

GMAIL_EMAIL = ""
GMAIL_APP_PASSWORD = ""

DEEPSEEK_API_KEY = ""
SERVERCHAN_SENDKEY = ""

PROFILE_PATH = "my_profile.json"
FETCH_HOURS = 24
MAX_EMAILS_PER_ACCOUNT = 20
# =====================================


def load_profile():
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def build_system_prompt(profile):
    """把画像拼成固定的 system prompt。这段内容每次完全一样，是缓存前缀。"""
    interests = "\n".join(f"- {x}" for x in profile["interests"])
    ignore = "\n".join(f"- {x}" for x in profile["ignore"])
    return f"""你是我的私人邮件助理。以下是我的个人画像和判断规则。

【个人画像】
{profile['description']}

【我关注的】
{interests}

【我不关注的】
{ignore}

【重要性分级】
- 🔴 需要行动：有截止日期、需要回复、涉及账号安全/付款/学校事务、实习申请等。
- 🟡 值得知道：与我的专业、求职、学习相关，但不需要立刻行动。
- ⚪ 可忽略：营销推广、其他大学本科招生、无关活动、社交平台通知等。

【输出格式】
## 🔴 需要行动
- [发件人] 主题：一句话说明 + 截止日期/行动项

## 🟡 值得知道
- [发件人] 主题：一句话说明

## ⚪ 已忽略
- 用一句话概括被忽略的邮件类型和数量，不要逐封列出。

【判断规则】
1. 与「我关注的」无关的邮件，一律归入「已忽略」。
2. 其他大学的本科招生、预科、夏校，除非与我的专业直接相关，一律忽略。
3. 电商、出行、游戏、社交平台营销邮件默认忽略；但若涉及账号安全或付款异常，归入「需要行动」。
4. 总字数控制在 400 字以内。
5. 如果没有任何重要邮件，只输出「今日无重要邮件」。
6. 「我关注的」邮件中，给出对应事件的超链接。
"""


from email.utils import parsedate_to_datetime

def fetch_emails_from_imap(server, port, username, password, hours=FETCH_HOURS):
    email_list = []
    cutoff = datetime.now().astimezone() - timedelta(hours=hours)

    try:
        mail = imaplib.IMAP4_SSL(server, port)
        mail.login(username, password)
        mail.select("inbox")

        since_date = (datetime.now() - timedelta(hours=hours)).strftime("%d-%b-%Y")
        status, messages = mail.search(None, f'(SINCE "{since_date}")')
        if status != "OK":
            print(f"[警告] {server} 搜索邮件失败")
            return []

        mail_ids = messages[0].split()
        mail_ids = mail_ids[-MAX_EMAILS_PER_ACCOUNT:]   # 只处理最近 N 封
        print(f"[调试] {server} 实际处理 {len(mail_ids)} 封")

        for i in mail_ids:
            status, msg_data = mail.fetch(i, "(RFC822)")
            for response_part in msg_data:
                if not isinstance(response_part, tuple):
                    continue
                msg = email.message_from_bytes(response_part[1])

                # 用邮件自带的 Date 头做精确时间过滤
                raw_date = msg.get("Date")
                try:
                    mail_dt = parsedate_to_datetime(raw_date)
                    if mail_dt.tzinfo is None:
                        mail_dt = mail_dt.replace(tzinfo=cutoff.tzinfo)
                    if mail_dt < cutoff:
                        continue  # 早于 24 小时，跳过
                except Exception:
                    pass  # 日期解析失败就保留，避免漏邮件

                # 以下照旧：解码主题、发件人、正文……
                subject_parts = decode_header(msg["Subject"] or "")
                subject = "".join(
                    safe_decode(part, (enc,) if enc else ()) if isinstance(part, bytes) else part
                    for part, enc in subject_parts
                )
                sender = safe_decode(msg.get("From", ""))

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = safe_decode(part.get_payload(decode=True))
                            break
                else:
                    body = safe_decode(msg.get_payload(decode=True))

                email_list.append({
                    "from": sender,
                    "subject": subject,
                    "body": body[:800],
                })

        mail.logout()
    except Exception as e:
        print(f"[错误] 读取 {server} 邮件失败: {e}")

    # 按时间倒序，最多保留 MAX_EMAILS_PER_ACCOUNT 封
    return email_list[:MAX_EMAILS_PER_ACCOUNT]

def safe_decode(value, encodings=("utf-8", "gbk", "gb18030", "big5", "latin-1")):
    """安全解码邮件头部或正文，自动尝试常见中文编码"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    for enc in encodings:
        try:
            return value.decode(enc, errors="ignore")
        except (LookupError, UnicodeDecodeError):
            continue
    return value.decode("utf-8", errors="ignore")

def prefilter(emails, profile):
    """规则层预过滤：在发给 LLM 之前先丢掉明确无关的邮件"""
    blacklist_senders = [s.lower() for s in profile.get("blacklist_senders", [])]
    blacklist_keywords = profile.get("blacklist_keywords", [])
    whitelist_senders = [s.lower() for s in profile.get("whitelist_senders", [])]

    kept, dropped = [], 0
    for e in emails:
        sender_lower = e["from"].lower()

        # 白名单发件人直接放行，避免误伤账号安全类邮件
        if any(w in sender_lower for w in whitelist_senders):
            kept.append(e)
            continue

        text = (e["from"] + e["subject"]).lower()
        if any(s in text for s in blacklist_senders):
            dropped += 1
            continue
        if any(k in e["subject"] for k in blacklist_keywords):
            dropped += 1
            continue

        kept.append(e)

    print(f"[预过滤] 保留 {len(kept)} 封，丢弃 {dropped} 封明显无关邮件")
    return kept


def summarize_emails(emails, system_prompt):
    """调用 DeepSeek 生成分类摘要"""
    if not emails:
        return "过去24小时没有新邮件。"

    text = "\n---\n".join([
        f"发件人: {e['from']}\n主题: {e['subject']}\n内容: {e['body']}"
        for e in emails
    ])

    # 关键：system 消息固定不变，作为缓存前缀；动态邮件列表放在 user 消息里
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"以下是今天的邮件列表：\n\n{text}"},
    ]

    resp = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
        json={
            "model": "deepseek-chat",
            "messages": messages,
            "temperature": 0.3,
        },
        timeout=60,
    )
    data = resp.json()

    if "choices" not in data:
        print(f"[错误] DeepSeek 返回异常: {data}")
        return "摘要生成失败，请检查 DeepSeek API。"

    return data["choices"][0]["message"]["content"]


def push_to_wechat(title, content):
    requests.post(
        f"https://sctapi.ftqq.com/{SERVERCHAN_SENDKEY}.send",
        data={"title": title, "desp": content},
        timeout=30,
    )


if __name__ == "__main__":
    profile = load_profile()
    system_prompt = build_system_prompt(profile)

    print("正在读取 QQ 邮箱...")
    qq_emails = fetch_emails_from_imap(
        "imap.qq.com", 993, QQ_EMAIL, QQ_AUTH_CODE
    )

    print("正在读取 Gmail...")
    gmail_emails = fetch_emails_from_imap(
        "imap.gmail.com", 993, GMAIL_EMAIL, GMAIL_APP_PASSWORD
    )

    all_emails = qq_emails + gmail_emails
    print(f"共获取 {len(all_emails)} 封邮件（QQ: {len(qq_emails)}, Gmail: {len(gmail_emails)}）")

    # 规则预过滤
    filtered = prefilter(all_emails, profile)

    if not filtered:
        push_to_wechat("📬 今日邮件摘要", "今日无重要邮件。")
        print("无邮件可总结，已推送空摘要。")
    else:
        summary = summarize_emails(filtered, system_prompt)
        push_to_wechat("📬 今日邮件摘要", summary)
        print("已推送摘要：")
        print(summary)

