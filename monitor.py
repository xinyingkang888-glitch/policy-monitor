"""Free, bounded official-page change monitor. No paid API or AI dependency."""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, urlopen

ROOT = Path(__file__).resolve().parent
CST = timezone(timedelta(hours=8))
MAX_BYTES = 4_000_000


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.active = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            a = dict(attrs)
            self.active = [a.get("href", ""), a.get("title", ""), []]

    def handle_data(self, data):
        if self.active:
            self.active[2].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.active:
            href, title, parts = self.active
            label = re.sub(r"\s+", " ", title or "".join(parts)).strip()
            self.items.append((href, label))
            self.active = None

    def handle_comment(self, data):
        # Some government sites place static article lists inside CMS comments.
        if "<a " in data.lower():
            p = Links()
            p.feed(data)
            self.items.extend(p.items)


def canonical(base, href):
    url = urljoin(base, html.unescape(href))
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    host = p.hostname.lower()
    if not (host.endswith(".gov.cn") or host in ("www.shzfgjj.cn", "www.scsjgjj.cn")):
        return None
    return urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))


def extract_articles(source, text):
    parser = Links()
    parser.feed(text)
    result = {}
    for href, title in parser.items:
        url = canonical(source["url"], href)
        if not url or len(title) < 10 or len(title) > 260:
            continue
        path = urlsplit(url).path.lower()
        # Exclude navigation, listing pages and downloads rather than claiming to read them.
        if not re.search(r"\.(s?html?|htm)$", path) or re.search(r"/(index|list)[^/]*\.", path):
            continue
        title = re.sub(r"\s*20\d{2}[-./年]\d{1,2}[-./月]\d{1,2}日?\s*$", "", title).strip()
        result[url] = {"url": url, "title": title}
    return list(result.values())


def fetch_source(source):
    for attempt in range(2):
        try:
            request = Request(source["url"], headers={"User-Agent": "Mozilla/5.0 (compatible; PersonalPolicyMonitor/1.0)"})
            with urlopen(request, timeout=20) as response:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("oversize")
                header_charset = response.headers.get_content_charset()
            match = re.search(br"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", raw[:8192])
            charset = header_charset or (match.group(1).decode("ascii") if match else "utf-8")
            text = raw.decode(charset, errors="replace")
            items = extract_articles(source, text)
            if len(items) < 3:
                return source, [], "未读取到足够文章：可能为动态网页、验证码或栏目结构变化"
            return source, items, None
        except Exception:
            if attempt == 0:
                time.sleep(1)
    return source, [], "连接失败或网页无法解析（已重试）；不能视为没有政策变化"


def identity(item):
    return hashlib.sha256((item["url"] + "\n" + item["title"]).encode()).hexdigest()


def categories(item, config):
    title = item["title"]
    return [c for c in config["categories"] if any(word in title for word in c["keywords"])]


def collect(state, results, config, now):
    """Pure transition for testing; first successful source read is a baseline."""
    hits, baselines, failed = [], [], []
    seen_global = state.setdefault("notified_items", {})
    for source, items, error in results:
        if error:
            failed.append(source["name"] + "：" + error)
            continue
        entry = state.setdefault("sources", {}).setdefault(source["id"], {"seen": {}, "initialized": False})
        initial = not entry["initialized"]
        if initial:
            baselines.append(source["name"])
        for item in items:
            key = identity(item)
            matched = categories(item, config)
            if key not in entry["seen"] and key not in seen_global and matched:
                if not initial:
                    hits.append({**item, "source": source["name"], "categories": matched})
                    seen_global[key] = now
            entry["seen"][key] = now
        entry["initialized"] = True
        entry["last_success"] = now
    # Same article may appear on several monitored pages.
    unique = {identity(item): item for item in hits}
    return list(unique.values()), baselines, failed


def safe_text(text):
    return re.sub(r"[\[\]<>*\x00-\x1f]", "", text)


def scan(root=ROOT, force=False):
    config = read_json(root / "config.json", {})
    state_path = root / "data/state.json"
    state = read_json(state_path, {"version": 1, "sources": {}, "outbox": [], "notified_items": {}})
    now = datetime.now(CST)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    month = now.strftime("%Y-%m")
    # Do not overwrite an undelivered digest; deliver it before collecting again.
    if state.get("outbox"):
        print("An earlier digest is pending; delivery will be retried first.")
        return
    if not force and state.get("complete_month") == month:
        print("This month's scan already completed. No repeat scan needed.")
        return
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch_source, config["sources"]))
    hits, baselines, failed = collect(state, results, config, stamp)
    report_path = "reports/" + now.strftime("%Y-%m-%d-%H%M%S") + ".md"
    report_url = "https://github.com/" + config["repository"] + "/blob/main/" + report_path
    lines = ["# 政策监测记录", "", "北京时间：" + stamp, "",
             config["scope_note"], "", "## 来源读取情况", ""]
    for source, items, error in results:
        status = error or ("已读取 " + str(len(items)) + " 个文章链接")
        lines.append("- " + safe_text(source["name"]) + "：" + status)
    lines += ["", "## 新发现的待核实线索", ""]
    if not hits:
        lines.append("本次未发现新增且标题命中规则的线索；不表示不存在适用政策或所有来源均已检查成功。")
    for item in hits:
        lines += ["", "### " + safe_text(item["title"]), "", item["url"],
                  "", "来源：" + safe_text(item["source"]), "",
                  "发现时间：" + stamp + "（不是发布日期或生效日期）", "",
                  "关联原因：" + "；".join(c["reason"] for c in item["categories"]), "",
                  "下一步：打开官方原文核实适用范围、材料、生效日和截止日；本程序不作资格认定。"]
    if baselines:
        lines += ["", "## 首次读取的来源", "",
                  "以下来源本次只建立历史基线，已有文章不会作为新政策推送。之后发现的新链接或标题变化才触发提醒。",
                  "", "、".join(baselines), "", "### 基线中的关键词匹配文章（不是新增政策）", ""]
        initial_names = set(baselines)
        for source, items, error in results:
            if source["name"] in initial_names:
                for item in items:
                    if categories(item, config):
                        lines.append("- " + safe_text(item["title"]) + " — " + item["url"])
    lines += ["", "## 已知覆盖限制", ""] + ["- " + gap for gap in config["coverage_gaps"]]
    report = "\n".join(lines) + "\n"
    (root / "reports").mkdir(exist_ok=True)
    (root / report_path).write_text(report, encoding="utf-8")
    (root / "latest-report.md").write_text(report, encoding="utf-8")
    initial_run = not state.get("activation_recorded")
    error_signature = month + "|" + "|".join(sorted(failed))
    alert_error = bool(failed) and error_signature != state.get("last_error_alert")
    recovered = not failed and bool(state.get("last_failed"))
    if hits or initial_run or alert_error or recovered:
        title = ("政策小灵通：首次监测记录" if initial_run else
                 "政策小灵通：" + (str(len(hits)) + "条待核实线索" if hits else
                               ("部分来源读取失败" if failed else "来源读取已恢复")))
        body = ["北京时间：" + stamp, "",
                "成功读取 " + str(len(results) - len(failed)) + "/" + str(len(results)) + " 个来源。", ""]
        if initial_run:
            body += ["本次建立基线，不把既有文章当作新政策。每月1日检查，2日和3日用于失败补查。",
                     "已有政策线索见完整报告；此通知不代表所有来源均已通过验证。", ""]
        for item in hits[:8]:
            body += ["- " + safe_text(item["title"]), "  " + item["url"]]
        if len(hits) > 8:
            body += ["其余线索请查看完整报告。"]
        if failed:
            body += ["", "未完成检查的来源："] + ["- " + x for x in failed]
        body += ["", "[查看完整报告（需登录你的GitHub账号）](" + report_url + ")", "",
                 "仅按标题规则筛选，可能漏检；是否适用、发布日期及办理期限以官方原文为准。"]
        state.setdefault("outbox", []).append({"title": title, "body": "\n".join(body), "created_at": stamp})
        if failed:
            state["last_error_alert"] = error_signature
    state["activation_recorded"] = True
    state["last_failed"] = failed
    state["last_attempt"] = stamp
    if not failed:
        state.pop("last_error_alert", None)
        state["complete_month"] = month
    write_json(state_path, state)
    write_json(root / "data/health.json", {"checked_at": stamp, "failed": failed, "sources": len(results)})
    print("Scan completed: " + str(len(hits)) + " candidate(s); " + str(len(failed)) + " failed source(s).")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def notify(title, body):
    key = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if not re.fullmatch(r"SCT[0-9A-Za-z]+", key):
        raise RuntimeError("Missing or invalid SERVERCHAN_SENDKEY.")
    request = Request("https://sctapi.ftqq.com/" + key + ".send",
                      data=urlencode({"title": title, "desp": body}).encode(),
                      headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            result = json.loads(response.read(65536).decode())
        if result.get("code") != 0 or result.get("data", {}).get("error") != "SUCCESS":
            raise ValueError()
    except Exception:
        # Never print the request URL, SendKey or raw provider exception.
        raise RuntimeError("ServerChan did not confirm acceptance. Check account/quota; pending message retained.") from None


def send(root=ROOT):
    path = root / "data/state.json"
    state = read_json(path, {})
    if not state.get("outbox"):
        print("No notification required.")
        return
    message = state["outbox"][0]
    notify(message["title"], message["body"])
    state["outbox"].pop(0)
    state["last_send_accepted"] = datetime.now(CST).isoformat()
    write_json(path, state)
    print("ServerChan accepted the notification; handset delivery is not verified.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["probe", "scan", "send", "health"])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.command == "probe":
        config = read_json(ROOT / "config.json", {})
        with ThreadPoolExecutor(max_workers=4) as pool:
            for source, items, error in pool.map(fetch_source, config["sources"]):
                print(source["id"] + ": " + (error or (str(len(items)) + " article links")))
        return
    if args.command == "scan":
        scan(force=args.force)
    elif args.command == "send":
        send()
    else:
        health = read_json(ROOT / "data/health.json", {"failed": ["No scan record"]})
        if health.get("failed"):
            print("Coverage incomplete. Open latest-report.md for failed sources.")
            sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR: " + (str(e) if isinstance(e, RuntimeError) else "Monitor failed; check configuration and files."))
        sys.exit(1)
