#!/usr/bin/env python3
"""
Visual regression checker.

Сравнивает скриншоты эталонного сайта и сайта с изменениями
в нескольких viewport-ах. Поддерживает мокирование бэкенд-запросов
и генерирует HTML-отчёт с расхождениями.
"""

import asyncio
import sys
import datetime as dt
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit, urljoin

import yaml
from PIL import Image, ImageChops, ImageDraw
from jinja2 import Template
from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Page,
    Route,
)


# ---------------------------------------------------------------- constants

# CSS-заморозка. Применяется многократно (до и после прокрутки).
FREEZE_CSS = """
*, *::before, *::after {
    animation-duration: 0s !important;
    animation-delay: 0s !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0s !important;
    transition-delay: 0s !important;
    caret-color: transparent !important;
    scroll-behavior: auto !important;
}

/* видео и аудио полностью скрываем — poster-кадр не детерминирован */
video, audio {
    visibility: hidden !important;
}

/* анимированные GIF/APNG — скрываем, они не подчиняются CSS */
img[src$=".gif"], img[src*=".gif?"],
img[src$=".apng"], img[src*=".apng?"] {
    visibility: hidden !important;
}

/* SMIL-анимации SVG */
svg animate, svg animateTransform, svg animateMotion {
    display: none !important;
}
"""

# JS-заморозка: пауза video, Web Animations API и остановка rAF.
# Вызывается дважды — до и после прокрутки.
FREEZE_JS = """
() => {
    // 1. Все <video> — на паузу, сброс на первый кадр
    document.querySelectorAll('video').forEach(v => {
        try {
            v.pause();
            v.removeAttribute('autoplay');
            v.currentTime = 0;
        } catch (e) {}
    });

    // 2. Web Animations API — пауза (ловит element.animate, включая Lottie)
    if (document.getAnimations) {
        document.getAnimations().forEach(a => {
            try { a.pause(); a.currentTime = 0; } catch (e) {}
        });
    }
}
"""

# Финальная заморозка — прямо перед скриншотом. Дополнительно глушит rAF.
FINAL_FREEZE_JS = """
() => {
    document.querySelectorAll('video').forEach(v => {
        try { v.pause(); } catch (e) {}
    });
    if (document.getAnimations) {
        document.getAnimations().forEach(a => {
            try { a.pause(); a.currentTime = 0; } catch (e) {}
        });
    }
    // после этого rAF больше не планирует новые кадры
    // (текущий кадр завершится, дальше тишина)
    window.requestAnimationFrame = function (cb) {
        try { cb(performance.now()); } catch (e) {}
        return 0;
    };
}
"""

SCROLL_JS = """
async () => {
    await new Promise(res => {
        let y = 0;
        const step = 400;
        const t = setInterval(() => {
            window.scrollTo(0, y);
            y += step;
            if (y >= document.body.scrollHeight) {
                clearInterval(t);
                window.scrollTo(0, 0);
                res();
            }
        }, 80);
    });
}
"""


# ---------------------------------------------------------------- utilities

def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def url_join(base: str, rel: str) -> str:
    """
    Корректно склеивает базовый URL (возможно, с query string)
    и относительный путь.
    """
    if rel.startswith(("http://", "https://")):
        return rel

    parts = urlsplit(base)
    new_path = urljoin(parts.path or "/", rel.lstrip("/"))
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        new_path,
        parts.query,
        parts.fragment,
    ))


def slug(rel: str) -> str:
    s = rel.strip("/").replace("/", "_").replace("?", "_").replace("=", "_")
    return s or "root"


def diff_images(ref_path: Path, cur_path: Path, out_path: Path) -> float:
    a = Image.open(ref_path).convert("RGB")
    b = Image.open(cur_path).convert("RGB")

    if a.size != b.size:
        w = max(a.width, b.width)
        h = max(a.height, b.height)
        canvas_a = Image.new("RGB", (w, h), "white")
        canvas_b = Image.new("RGB", (w, h), "white")
        canvas_a.paste(a, (0, 0))
        canvas_b.paste(b, (0, 0))
        a, b = canvas_a, canvas_b

    w, h = a.size
    total = w * h

    diff = ImageChops.difference(a, b)
    if diff.getbbox() is None:
        Image.new("RGB", (w, h), "white").save(out_path)
        return 0.0

    gray = diff.convert("L")
    hist = gray.histogram()
    changed = sum(hist[10:])
    ratio = changed / total if total else 0.0

    overlay = b.copy()
    draw = ImageDraw.Draw(overlay)
    px = gray.load()
    for y in range(h):
        for x in range(w):
            if px[x, y] > 25:
                draw.point((x, y), fill=(255, 0, 0))
    overlay.save(out_path)

    return ratio


# ------------------------------------------------------------------- mocks

async def apply_mocks(context: BrowserContext, mocks: dict[str, str]) -> None:
    def make_handler(file_path: str):
        async def handler(route: Route, request=None) -> None:
            try:
                body = Path(file_path).read_text(encoding="utf-8")
            except FileNotFoundError:
                print(f"  ! mock file not found: {file_path}", file=sys.stderr)
                await route.continue_()
                return
            except Exception as e:
                print(f"  ! mock error ({file_path}): {e}", file=sys.stderr)
                await route.continue_()
                return

            await route.fulfill(
                status=200,
                content_type="application/json",
                body=body,
            )
        return handler

    for pattern, file_path in mocks.items():
        await context.route(pattern, make_handler(file_path))


# ---------------------------------------------------------------- freezing

async def freeze_page(
    page: Page,
    mask_selectors: list[str],
    disable_animations: bool,
) -> None:
    """
    Многоуровневая заморозка страницы:
      1. CSS — обнуляет CSS-анимации и transition
      2. JS  — пауза <video> и Web Animations API
      3. маски — прячет элементы по селекторам

    Можно вызывать многократно (после прокрутки — обязательно).
    """
    if disable_animations:
        try:
            await page.add_style_tag(content=FREEZE_CSS)
        except Exception as e:
            print(f"  ! add_style_tag failed: {e}", file=sys.stderr)

        try:
            await page.evaluate(FREEZE_JS)
        except Exception as e:
            print(f"  ! freeze_js failed: {e}", file=sys.stderr)

    for sel in mask_selectors:
        try:
            await page.evaluate(
                """(sel) => document.querySelectorAll(sel).forEach(el => {
                    el.style.visibility = 'hidden';
                })""",
                sel,
            )
        except Exception as e:
            print(f"  ! hide_dynamic({sel}) failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------- rendering

async def take_screenshot(
    base_url: str,
    rel_url: str,
    viewport: dict,
    out_path: Path,
    mocks: dict[str, str],
    settle_ms: int,
    mask_selectors: list[str],
    disable_animations: bool,
) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": viewport["width"], "height": viewport["height"]},
            device_scale_factor=1,
            ignore_https_errors=True,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124 Safari/537.36"
            ),
        )
        await apply_mocks(context, mocks)

        page = await context.new_page()
        target = url_join(base_url, rel_url)

        try:
            await page.goto(target, wait_until="load", timeout=60_000)
        except Exception as e:
            print(f"  ! goto failed {target}: {e}", file=sys.stderr)

        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        # первая заморозка — до прокрутки
        await freeze_page(page, mask_selectors, disable_animations)
        await page.wait_for_timeout(settle_ms)

        # прокрутка для lazy-load
        try:
            await page.evaluate(SCROLL_JS)
        except Exception as e:
            print(f"  ! scroll failed: {e}", file=sys.stderr)

        # вторая заморозка — scroll мог оживить анимации
        await freeze_page(page, mask_selectors, disable_animations)

        # финальный «стоп-кран»: пауза всех анимаций и остановка rAF
        if disable_animations:
            try:
                await page.evaluate(FINAL_FREEZE_JS)
            except Exception as e:
                print(f"  ! final_freeze failed: {e}", file=sys.stderr)

        # дать текущему кадру завершиться
        await page.wait_for_timeout(300)

        await page.screenshot(path=str(out_path), full_page=True)
        await browser.close()


# --------------------------------------------------------------- main loop

async def check_one(
    rel_url: str,
    vp_name: str,
    viewport: dict,
    cfg: dict,
    outdir: Path,
) -> dict[str, Any]:
    name = slug(rel_url)
    ref_png = outdir / f"{name}__{vp_name}__ref.png"
    cur_png = outdir / f"{name}__{vp_name}__cur.png"
    diff_png = outdir / f"{name}__{vp_name}__diff.png"

    print(f"→ [{vp_name}] {rel_url}")

    await take_screenshot(
        cfg["reference"], rel_url, viewport, ref_png,
        cfg.get("mocks", {}), cfg["settle_ms"],
        cfg.get("mask_selectors", []), cfg["disable_animations"],
    )
    await take_screenshot(
        cfg["current"], rel_url, viewport, cur_png,
        cfg.get("mocks", {}), cfg["settle_ms"],
        cfg.get("mask_selectors", []), cfg["disable_animations"],
    )

    ratio = diff_images(ref_png, cur_png, diff_png)
    status = "ok" if ratio <= cfg["diff_threshold"] else "diff"
    print(f"   {'✓' if status == 'ok' else '✗'} diff={ratio:.4%}")

    return {
        "url": rel_url,
        "viewport": vp_name,
        "diff_ratio": ratio,
        "status": status,
        "ref": ref_png.name,
        "cur": cur_png.name,
        "diff": diff_png.name,
    }


REPORT_TEMPLATE = """
<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Visual diff report — {{ ts }}</title>
<style>
  body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:24px;
       background:#fafafa;color:#222}
  h1{margin:0 0 4px}
  .meta{color:#666;margin-bottom:24px}
  table{border-collapse:collapse;width:100%;background:#fff;
        box-shadow:0 1px 3px rgba(0,0,0,.08)}
  th,td{padding:10px 12px;border-bottom:1px solid #eee;text-align:left;
        vertical-align:top}
  th{background:#f4f4f4;font-weight:600}
  .ok{color:#197d29;font-weight:600}
  .diff{color:#c9261c;font-weight:600}
  .pair{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
  .pair figure{margin:0}
  .pair img{max-width:100%;border:1px solid #ddd;border-radius:4px;
            background:#fff}
  figcaption{font-size:12px;color:#666;margin-top:4px}
  details summary{cursor:pointer;color:#0563c1}
</style></head>
<body>
  <h1>Отчёт визуальной регрессии</h1>
  <div class="meta">Сформирован: {{ ts }} · Порог: {{ threshold_pct }}% ·
     Всего проверок: {{ results|length }} ·
     Расхождений: {{ broken }}</div>

  <table>
    <tr><th>URL</th><th>Viewport</th><th>Diff %</th><th>Статус</th>
        <th>Детали</th></tr>
    {% for r in results %}
    <tr>
      <td>{{ r.url }}</td>
      <td>{{ r.viewport }}</td>
      <td>{{ "%.4f"|format(r.diff_ratio*100) }}%</td>
      <td class="{{ r.status }}">{{ "OK" if r.status=="ok" else "DIFF" }}</td>
      <td>
        <details>
          <summary>Открыть</summary>
          <div class="pair">
            <figure><img src="{{ r.ref }}"><figcaption>Эталон</figcaption></figure>
            <figure><img src="{{ r.cur }}"><figcaption>Текущий</figcaption></figure>
            <figure><img src="{{ r.diff }}"><figcaption>Различия (красным)</figcaption></figure>
          </div>
        </details>
      </td>
    </tr>
    {% endfor %}
  </table>
</body></html>
"""


async def main(cfg_path: str = "config.yaml") -> int:
    cfg = load_config(cfg_path)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = Path("reports") / ts
    outdir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []

    for rel_url in cfg["urls"]:
        for vp_name, vp in cfg["viewports"].items():
            try:
                results.append(
                    await check_one(rel_url, vp_name, vp, cfg, outdir)
                )
            except Exception as e:
                print(f"   ! FAILED {rel_url} [{vp_name}]: {e}",
                      file=sys.stderr)
                results.append({
                    "url": rel_url,
                    "viewport": vp_name,
                    "diff_ratio": 1.0,
                    "status": "diff",
                    "ref": "",
                    "cur": "",
                    "diff": "",
                })

    broken = sum(1 for r in results if r["status"] != "ok")
    html = Template(REPORT_TEMPLATE).render(
        ts=ts,
        results=results,
        threshold_pct=cfg["diff_threshold"] * 100,
        broken=broken,
    )
    report = outdir / "index.html"
    report.write_text(html, encoding="utf-8")

    print(f"\nОтчёт: {report.resolve()}")
    print(f"Проверок: {len(results)}, расхождений: {broken}")
    return 0 if broken == 0 else 1


if __name__ == "__main__":
    cfg = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    sys.exit(asyncio.run(main(cfg)))