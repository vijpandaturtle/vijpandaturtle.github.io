#!/usr/bin/env python3
"""Import posts from the Mind & AI Substack into site/blog/.

Re-run after publishing a new Substack post:

    python3 scripts/import_substack.py

It reads the Substack RSS feed, cleans each post's HTML, downloads its images
next to the post, writes site/blog/<slug>/index.html plus the site/blog/ index,
and refreshes the "Latest writing" block on the homepage. Needs beautifulsoup4.
"""
import email.utils
import hashlib
import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from bs4 import BeautifulSoup, Comment

FEED = "https://mindsandai.substack.com/feed"
SUBSTACK = "https://mindsandai.substack.com"
SITE_URL = "https://vijpandaturtle.github.io"
ROOT = Path(__file__).resolve().parent.parent / "site"
BLOG = ROOT / "blog"
NS = {"content": "http://purl.org/rss/1.0/modules/content/"}
UA = {"User-Agent": "Mozilla/5.0 (substack-import for vijpandaturtle.github.io)"}

# Posts that belong to a series are grouped on the blog index.
SERIES = [
    (r"^Building Seesaw", "Building Seesaw: Agentic AI for AI Safety Research", "building-seesaw"),
    (r"^Navigating the Landscape of AI Alignment", "Navigating the Landscape of AI Alignment", "ai-alignment"),
]


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read(), r.headers.get("Content-Type", "")


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def series_of(title):
    for pattern, name, key in SERIES:
        if re.search(pattern, title):
            part = re.search(r"Part\s+(\d+)", title)
            return name, key, int(part.group(1)) if part else None
    return None, None, None


def make_slug(title):
    _, key, part = series_of(title)
    if key and part:
        return f"{key}-part-{part}"
    return slugify(title.replace("AI Safety:", "").replace(" : ", " "))[:60].strip("-")


def download_image(url, dest_dir):
    """Save an image next to the post and return its file name."""
    name = hashlib.sha1(url.encode()).hexdigest()[:12]
    for existing in dest_dir.glob(name + ".*"):
        return existing.name
    data, ctype = fetch(url)
    ext = {"image/png": ".png", "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg"}.get(
        ctype.split(";")[0].strip(), ".jpg")
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / (name + ext)).write_bytes(data)
    return name + ext


def best_image_url(img):
    """Prefer a 1456px Substack CDN rendition; fall back to the src."""
    src = img.get("src", "")
    try:
        attrs = json.loads(img.get("data-attrs") or "{}")
    except ValueError:
        attrs = {}
    original = attrs.get("src")
    if original:
        return ("https://substackcdn.com/image/fetch/w_1456,c_limit,f_auto,q_auto:good,fl_progressive:steep/"
                + urllib.parse.quote(original, safe=""))
    return src


def clean(content_html, post_dir, post_url, slug_map):
    soup = BeautifulSoup(content_html, "html.parser")

    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for sel in [".subscription-widget-wrap-editor", ".subscription-widget", "form", "button",
                ".image-link-expand", "svg", "script", "style", ".button-wrapper"]:
        for el in soup.select(sel):
            el.decompose()

    # Images: replace Substack's picture/figure markup with a plain figure + local file.
    for box in soup.select(".captioned-image-container"):
        img = box.find("img")
        if not img:
            box.decompose()
            continue
        fname = download_image(best_image_url(img), post_dir)
        cap = box.find("figcaption")
        fig = soup.new_tag("figure")
        new_img = soup.new_tag("img", src=fname, loading="lazy", alt=img.get("alt") or (cap.get_text(" ", strip=True) if cap else ""))
        if img.get("width") and img.get("height"):
            new_img["width"], new_img["height"] = img["width"], img["height"]
        fig.append(new_img)
        if cap and cap.get_text(strip=True):
            fc = soup.new_tag("figcaption")
            fc.append(BeautifulSoup(cap.decode_contents(), "html.parser"))
            fig.append(fc)
        box.replace_with(fig)
    for img in soup.find_all("img"):
        if not img["src"].startswith(("http", "data:")) or img.find_parent("figure"):
            continue
        img["src"] = download_image(best_image_url(img), post_dir)
        img["loading"] = "lazy"

    # Substack-hosted videos can't be embedded elsewhere; point readers to the original.
    for v in soup.select(".native-video-embed, .video-embed, .youtube-wrap"):
        p = soup.new_tag("p", attrs={"class": "embed-note"})
        a = soup.new_tag("a", href=post_url)
        a.string = "Watch the video in the original post on Substack →"
        p.append(a)
        v.replace_with(p)

    # Code blocks: keep <pre><code class="language-x">, drop wrappers.
    for block in soup.select(".highlighted_code_block"):
        pre = block.find("pre")
        block.replace_with(pre) if pre else block.decompose()

    # Links to other posts in this publication now point at the local copy.
    for a in soup.find_all("a", href=True):
        # Also catches the publication's old address (aisafetydecoded.substack.com).
        m = re.match(r"https?://[a-z0-9-]+\.substack\.com/p/([^/?#]+)", a["href"])
        if m and m.group(1) in slug_map:
            a["href"] = f"/blog/{slug_map[m.group(1)]}/"
        elif a["href"].startswith("http"):
            a["target"], a["rel"] = "_blank", "noopener"

    # Strip Substack attributes and layout wrappers.
    for h1 in soup.find_all("h1"):
        h1.name = "h2"
    for el in soup.find_all(True):
        keep = {"href", "src", "alt", "width", "height", "loading", "target", "rel", "colspan", "rowspan", "start"}
        cls = [c for c in el.get("class", []) if c.startswith("language-") or c == "embed-note"]
        el.attrs = {k: v for k, v in el.attrs.items() if k in keep}
        if cls:
            el["class"] = cls
    for el in soup.find_all(["span", "div", "picture", "source"]):
        if el.name == "source":
            el.decompose()
        else:
            el.unwrap()
    for p in soup.find_all("p"):
        if not p.get_text(strip=True) and not p.find("img"):
            p.decompose()
    return str(soup)


def page(title, description, canonical, body, extra_head=""):
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#dceaff">
  <title>{html.escape(title)}</title>
  <meta name="description" content="{html.escape(description)}">
  <meta property="og:title" content="{html.escape(title)}">
  <meta property="og:description" content="{html.escape(description)}">
  <meta property="og:url" content="{canonical}">
  <link rel="canonical" href="{canonical}">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;1,400&family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/style.css">{extra_head}
</head>
<body>
  <header class="nav">
    <div class="wrap nav-inner">
      <a class="brand" href="/">Vijayasri Iyer<span>.</span></a>
      <nav>
        <a class="m" href="/#work">Work</a>
        <a href="/#experience">Experience</a>
        <a href="/#research">Research</a>
        <a class="m" href="/blog/">Writing</a>
        <a href="/#contact">Contact</a>
      </nav>
    </div>
  </header>
{body}
  <footer class="wrap footer">
    <span>© Vijayasri Iyer</span>
    <a href="{SUBSTACK}">Subscribe on Substack ↗</a>
  </footer>
</body>
</html>
"""


def fmt_date(d):
    return d.strftime("%b %-d, %Y")


def main():
    raw, _ = fetch(FEED)
    root = ET.fromstring(raw)
    posts = []
    for it in root.iter("item"):
        # Feed text can carry HTML entities (e.g. &#8217;); decode before re-escaping.
        title = html.unescape(it.findtext("title") or "").strip()
        link = it.findtext("link").strip()
        posts.append({
            "title": title,
            "subtitle": html.unescape(it.findtext("description") or "").strip(),
            "url": link,
            "substack_slug": link.rstrip("/").rsplit("/", 1)[-1],
            "date": email.utils.parsedate_to_datetime(it.findtext("pubDate")),
            "content": it.find("content:encoded", NS).text or "",
            "slug": make_slug(title),
        })
    posts.sort(key=lambda p: p["date"], reverse=True)
    slug_map = {p["substack_slug"]: p["slug"] for p in posts}

    for p in posts:
        p["series"], p["series_key"], p["part"] = series_of(p["title"])
        words = len(re.sub(r"<[^>]+>", " ", p["content"]).split())
        p["minutes"] = max(1, round(words / 230))

    for p in posts:
        post_dir = BLOG / p["slug"]
        body_html = clean(p["content"], post_dir, p["url"], slug_map)
        siblings = sorted([q for q in posts if p["series_key"] and q["series_key"] == p["series_key"]],
                          key=lambda q: q["part"] or 0)
        nav = ""
        if siblings:
            i = siblings.index(p)
            prev_ = siblings[i - 1] if i > 0 else None
            next_ = siblings[i + 1] if i + 1 < len(siblings) else None
            nav = '\n      <nav class="post-nav">'
            nav += (f'<a href="/blog/{prev_["slug"]}/"><span>← Previous</span>{html.escape(prev_["title"])}</a>'
                    if prev_ else "<span></span>")
            nav += (f'<a class="next" href="/blog/{next_["slug"]}/"><span>Next →</span>{html.escape(next_["title"])}</a>'
                    if next_ else "<span></span>")
            nav += "</nav>"
        series_line = (f'<p class="post-series">Part {p["part"]} of <a href="/blog/#{p["series_key"]}">{html.escape(p["series"])}</a></p>'
                       if p["series"] else "")
        body = f"""
  <main class="wrap post">
    <article>
      <header class="post-head">
        <p class="post-back"><a href="/blog/">← All writing</a></p>
        {series_line}
        <h1>{html.escape(p["title"])}</h1>
        {f'<p class="post-sub">{html.escape(p["subtitle"])}</p>' if p["subtitle"] else ""}
        <p class="post-meta">{fmt_date(p["date"])} · {p["minutes"]} min read</p>
      </header>
      <div class="prose">
{body_html}
      </div>
      <p class="post-origin">Originally published on <a href="{p["url"]}">Mind &amp; AI on Substack</a>.</p>{nav}
    </article>
  </main>
"""
        post_dir.mkdir(parents=True, exist_ok=True)
        (post_dir / "index.html").write_text(
            page(f'{p["title"]} · Vijayasri Iyer', p["subtitle"] or p["title"],
                 f'{SITE_URL}/blog/{p["slug"]}/', body))
        print("wrote", post_dir.relative_to(ROOT))

    # Blog index: series first (newest series on top), then standalone posts.
    groups, seen = [], set()
    for p in posts:
        if p["series_key"] and p["series_key"] not in seen:
            seen.add(p["series_key"])
            groups.append((p["series"], p["series_key"],
                           sorted([q for q in posts if q["series_key"] == p["series_key"]], key=lambda q: q["part"] or 0)))
    standalone = [p for p in posts if not p["series_key"]]

    def item(p, label=None):
        excerpt = f'<span class="post-excerpt">{html.escape(p["subtitle"])}</span>' if p["subtitle"] else ""
        return (f'<li><a href="/blog/{p["slug"]}/"><span class="post-date">{label or fmt_date(p["date"])}</span>'
                f'<span class="post-title">{html.escape(p["title"])}</span>{excerpt}</a></li>')

    sections = ""
    for name, key, items in groups:
        sections += f'\n    <section id="{key}" class="post-group">\n      <h2>{html.escape(name)}</h2>\n      <ul class="post-list">\n'
        sections += "\n".join("        " + item(q) for q in items)
        sections += "\n      </ul>\n    </section>\n"
    if standalone:
        sections += '\n    <section class="post-group">\n      <h2>Other posts</h2>\n      <ul class="post-list">\n'
        sections += "\n".join("        " + item(q) for q in standalone)
        sections += "\n      </ul>\n    </section>\n"
    index_body = f"""
  <main class="wrap blog">
    <section class="blog-head">
      <h1>Writing</h1>
      <p class="lede">Notes on AI safety, interpretability, alignment and inference engineering. Also on <a href="{SUBSTACK}">Substack</a>.</p>
    </section>{sections}
  </main>
"""
    (BLOG / "index.html").write_text(
        page("Writing · Vijayasri Iyer", "Essays and build logs by Vijayasri Iyer on AI safety, interpretability and inference engineering.",
             f"{SITE_URL}/blog/", index_body))
    print("wrote blog/index.html")

    # Homepage "Latest writing" block.
    home = ROOT / "index.html"
    text = home.read_text()
    latest = "\n".join(
        f'          <li><a href="blog/{p["slug"]}/"><span class="post-date">{fmt_date(p["date"])}</span>'
        f'<span class="post-title">{html.escape(p["title"])}</span></a></li>' for p in posts[:4])
    block = f'<!-- latest-posts:start -->\n        <ul class="post-list compact">\n{latest}\n        </ul>\n        <!-- latest-posts:end -->'
    text, n = re.subn(r"<!-- latest-posts:start -->.*?<!-- latest-posts:end -->", block, text, flags=re.S)
    if n:
        home.write_text(text)
        print("updated homepage latest posts")


if __name__ == "__main__":
    main()
