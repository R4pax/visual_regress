#!/usr/bin/env python3
"""
Visual regression checker.

Сравнивает скриншоты эталонного сайта и сайта с изменениями
в нескольких viewport-ах. Поддерживает мокирование бэкенд-запросов
и генерирует HTML-отчёт с расхождениями.
"""

import asyncio
import ipaddress
import json
import sys
import time
import datetime as dt
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit, urljoin

import yaml
from PIL import Image, ImageChops, ImageDraw
from jinja2 import Template
from playwright.async_api import (
    async_playwright,
    BrowserContext,
    CDPSession,
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

/* видео и аудио полностью скрываем — poster-кадр не детерминирован 
video, audio {
    visibility: hidden !important;
}
*/

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

# Финальная заморозка — прямо перед скриншотом. Видео и CSS/WAAPI-анимации.
# rAF гасится в HARD_FREEZE_JS (там же глушатся слушатели и observers).
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
}
"""

# «Шумные» события: именно они оживляют страницу после прокрутки —
# скролл-хендлеры, параллаксы, sticky-хедеры, mouse/pointer-tracking.
# Сознательно НЕ блокируем load/DOMContentLoaded/animationend/transitionend:
# часть библиотек ждёт их для инициализации, блокировка ломала бы вёрстку.
DEFAULT_EVENT_TYPES = [
    "scroll", "wheel", "mousewheel",
    "touchstart", "touchmove", "touchend", "touchcancel",
    "pointermove", "pointerdown", "pointerup", "pointercancel",
    "mousemove", "mousedown", "mouseup", "mouseover", "mouseout",
    "mouseenter", "mouseleave",
    "keydown", "keyup", "keypress",
    "resize", "orientationchange",
    "drag", "dragstart", "dragenter", "dragover", "dragleave", "dragend", "drop",
]

# GUARD_SCRIPT ставится через context.add_init_script ДО скриптов страницы
# и строит window.__freeze — реестр, которым потом управляет HARD_FREEZE_JS.
# Он умеет:
#   • съедать регистрацию слушателей по «шумным» событиям (когда взведён);
#   • вести реестр observer'ов, чтобы погасить их перед снимком;
#   • опционально вести реестр таймеров.
# Плейсхолдер __OPTIONS__ заменяется JSON'ом в build_guard_script().
GUARD_SCRIPT = r"""
(() => {
    const OPT = __OPTIONS__;
    const freeze = {
        events_armed: false,       // рубить ли новые listener'ы
        observers_armed: false,    // погашены ли observer'ы
        blocked: new Set((OPT.event_types || []).map(t => String(t).toLowerCase())),
        observers: [],
        timeouts: new Set(),
        intervals: new Set(),
    };
    Object.defineProperty(window, '__freeze', {
        value: freeze, configurable: true, writable: false,
    });

    /* 1. Слушатели событий: перехватываем регистрацию ------------------ */
    const nativeAdd = EventTarget.prototype.addEventListener;
    EventTarget.prototype.addEventListener = function (type, listener, options) {
        if (freeze.events_armed
            && typeof type === 'string'
            && freeze.blocked.has(type.toLowerCase())) {
            return;                        // тихо съедаем регистрацию
        }
        return nativeAdd.call(this, type, listener, options);
    };

    /* 2. Observers: реестр инстансов + подмена конструкторов ----------- */
    for (const name of ['IntersectionObserver', 'ResizeObserver', 'MutationObserver']) {
        const Native = window[name];
        if (typeof Native !== 'function') continue;
        function Frozen(callback, options) {
            const instance = new Native(callback, options);
            if (freeze.observers_armed) {
                try { instance.disconnect(); } catch (e) {}
            } else {
                freeze.observers.push(instance);
            }
            return instance;
        }
        Frozen.prototype = Native.prototype;
        try { window[name] = Frozen; } catch (e) {}
    }

    /* 3. Таймеры: реестр, чтобы погасить всё запланированное ------------ */
    if (OPT.timers) {
        const nativeSetTimeout = window.setTimeout;
        const nativeSetInterval = window.setInterval;
        const nativeClearTimeout = window.clearTimeout;
        const nativeClearInterval = window.clearInterval;

        window.setTimeout = function (handler, delay, ...args) {
            const id = nativeSetTimeout(() => {
                freeze.timeouts.delete(id);
                if (typeof handler === 'function') return handler.apply(window, args);
                return (0, eval)(handler);
            }, delay);
            freeze.timeouts.add(id);
            return id;
        };
        window.setInterval = function (handler, delay, ...args) {
            const id = nativeSetInterval(() => {
                if (typeof handler === 'function') return handler.apply(window, args);
                return (0, eval)(handler);
            }, delay);
            freeze.intervals.add(id);
            return id;
        };
        window.clearTimeout = function (id) {
            freeze.timeouts.delete(id);
            return nativeClearTimeout(id);
        };
        window.clearInterval = function (id) {
            freeze.intervals.delete(id);
            return nativeClearInterval(id);
        };
    }

    /* 4. Взводим предохранитель сразу, если попросили ------------------- */
    if (OPT.events && OPT.arm_events === 'start') {
        freeze.events_armed = true;
    }
})();
"""

# HARD_FREEZE_JS — вызывается в момент заморозки (после прокрутки).
# Взводит предохранитель, гасит живые observer'ы, снимает таймеры,
# превращает rAF в заглушку и глушит inline-обработчики вида el.onscroll.
HARD_FREEZE_JS = r"""
(opts) => {
    const noop = () => {};
    const freeze = window.__freeze;

    /* 1. Слушатели: новые регистрации и inline-обработчики ------------- */
    if (freeze && opts.events) {
        freeze.events_armed = true;
    }
    if (opts.events) {
        for (const target of [window, Document.prototype, Element.prototype]) {
            for (const ev of opts.event_types) {
                const prop = 'on' + String(ev).toLowerCase();
                try {
                    if (prop in target) {
                        Object.defineProperty(target, prop, {
                            configurable: true,
                            get: () => null,
                            set: () => {},
                        });
                    }
                } catch (e) {}
            }
        }
    }

    /* 2. Observers: гасим живые и подменяем конструкторы заглушками ---- */
    if (freeze && opts.observers) {
        freeze.observers_armed = true;
        for (const observer of freeze.observers) {
            try { observer.disconnect(); } catch (e) {}
        }
        freeze.observers.length = 0;
    }
    if (opts.observers) {
        for (const name of ['IntersectionObserver', 'ResizeObserver', 'MutationObserver']) {
            if (typeof window[name] !== 'function') continue;
            function Dead() {}
            Dead.prototype.observe = noop;
            Dead.prototype.unobserve = noop;
            Dead.prototype.disconnect = noop;
            Dead.prototype.takeRecords = () => [];
            try { window[name] = Dead; } catch (e) {}
        }
    }

    /* 3. Таймеры: снимаем запланированное и обнуляем API ---------------- */
    if (freeze && opts.timers) {
        for (const id of freeze.timeouts) {
            try { window.clearTimeout(id); } catch (e) {}
        }
        freeze.timeouts.clear();
        for (const id of freeze.intervals) {
            try { window.clearInterval(id); } catch (e) {}
        }
        freeze.intervals.clear();
    }
    if (opts.timers) {
        window.setTimeout = () => 0;
        window.setInterval = () => 0;
        window.clearTimeout = noop;
        window.clearInterval = noop;
    }

    /* 4. rAF: колбэк НЕ вызываем, иначе rAF-цикл уйдёт в синхронную
          рекурсию («tick → requestAnimationFrame(tick)») и переполнит стек */
    if (opts.raf) {
        window.requestAnimationFrame = () => 0;
        window.cancelAnimationFrame = noop;
    }
}
"""

# Снятие уже навешанных слушателей через DevTools-API getEventListeners
# (доступно только при includeCommandLineAPI=true).
STRIP_LISTENERS_JS = r"""
(types) => {
    const blocked = new Set(types.map(t => String(t).toLowerCase()));
    const scopes = [window, document, document.documentElement, document.body];
    let removed = 0;
    for (const scope of scopes) {
        if (!scope) continue;
        let map;
        try { map = getEventListeners(scope); } catch (e) { continue; }
        for (const type of Object.keys(map || {})) {
            if (!blocked.has(type.toLowerCase())) continue;
            for (const item of map[type]) {
                try {
                    scope.removeEventListener(type, item.listener, item.useCapture);
                    removed += 1;
                } catch (e) {}
            }
        }
    }
    return removed;
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

# Типы запросов, которые рубим у чужих доменов. `script` — это GTM,
# метрики, чаты, пиксели рекламы; `subdocument` — их же iframe'ы
# (Playwright отдаёт их как `document`, отличаем по родительскому фрейму).
# Стили/картинки/шрифты НЕ трогаем: без них поедет вёрстка и появятся
# ложные расхождения.
DEFAULT_BLOCKED_RESOURCE_TYPES = ["script", "subdocument"]

# Двухуровневые публичные суффиксы, которые встречаются чаще всего:
# чтобы cdn.client.co.uk не приняли за "co.uk".
MULTI_LABEL_SUFFIXES = {
    "co.uk", "org.uk", "com.ru", "org.ru", "net.ru", "msk.ru", "spb.ru",
    "com.au", "co.jp", "co.kr", "com.br", "co.com", "com.tr",
    "github.io", "pages.dev", "vercel.app", "netlify.app", "herokuapp.com",
    "appspot.com", "cloudfront.net", "azurewebsites.net",
}

# WAIT_MEDIA_JS — проверка реальной готовности ресурсов к отрисовке.
# `img.complete === true` НЕ значит, что картинка уже раскодирована и попадёт
# в снимок целиком (классика на progressive JPEG) — поэтому для каждой
# картинки вызывается img.decode(). Вызывается в цикле из wait_for_media().
WAIT_MEDIA_JS = r"""
async (opts) => {
    const perResource = (opts && opts.per_resource_ms) || 30000;
    const sampleLimit = (opts && opts.sample_limit) || 15;

    // true — ресурс успел; false — не успел за отведённое время
    const withTimeout = (promise) => Promise.race([
        promise.then(() => true, () => true),
        new Promise(resolve => setTimeout(() => resolve(false), perResource)),
    ]);

    const short = (url) => {
        const s = String(url || '');
        return s.length > 160 ? s.slice(0, 157) + '...' : s;
    };

    const check = async (kind, url, promise) => ({
        kind, url: short(url), ok: await withTimeout(promise),
    });

    const isHidden = (el) => {
        try {
            const cs = getComputedStyle(el);
            return cs.display === 'none' || cs.visibility === 'hidden';
        } catch (e) { return false; }
    };

    const waitImage = (img) => {
        // complete=true → браузер уже закончил с этой картинкой.
        // naturalWidth=0 при этом означает битый/пустой src: load/error
        // по ней БОЛЬШЕ НЕ сработают, и ждать её — верный таймаут.
        if (img.complete) {
            return (img.naturalWidth > 0 && img.decode)
                ? img.decode()
                : Promise.resolve();
        }
        return new Promise(resolve => {
            const finish = () => {
                img.removeEventListener('load', finish);
                img.removeEventListener('error', finish);
                resolve();
            };
            img.addEventListener('load', finish);
            img.addEventListener('error', finish);
            // гонка: картинка могла догрузиться между проверкой и подпиской
            if (img.complete) finish();
        });
    };

    // картинки из same-origin iframe тоже считаем
    const collectImages = () => {
        const out = Array.from(document.images || []);
        for (const frame of document.querySelectorAll('iframe')) {
            try {
                const doc = frame.contentDocument;
                if (doc) out.push(...Array.from(doc.images || []));
            } catch (e) {}   // cross-origin iframe — недоступен
        }
        return out;
    };

    /* 1. Картинки --------------------------------------------------- */
    const images = collectImages();
    const waiting = images.filter(img => !isHidden(img) && (img.currentSrc || img.src));
    const imageChecks = await Promise.all(waiting.map(img =>
        check('img', img.currentSrc || img.src, waitImage(img))));
    const pendingImages = imageChecks.filter(c => !c.ok).length;
    const pendingWaiting = imageChecks.filter(c => !c.ok);

    /* 2. CSS-фоны --------------------------------------------------- */
    let bgTotal = 0;
    let pendingBackgrounds = 0;
    if (opts && opts.backgrounds) {
        let urls;
        if (window.__bgUrls instanceof Set) {
            urls = window.__bgUrls;      // скан тяжёлый — делаем один раз
        } else {
            urls = new Set();
            const re = /url\(\s*(['"]?)(.*?)\1\s*\)/g;
            for (const el of document.querySelectorAll('*')) {
                if (isHidden(el)) continue;
                const bg = getComputedStyle(el).backgroundImage;
                if (!bg || bg === 'none') continue;
                re.lastIndex = 0;
                let m;
                while ((m = re.exec(bg))) {
                    const url = m[2];
                    if (url && !url.startsWith('data:')) urls.add(url);
                }
            }
            window.__bgUrls = urls;
        }
        bgTotal = urls.size;
        const bgChecks = await Promise.all(Array.from(urls).map(url =>
            check('bg', url, new Promise(resolve => {
                const probe = new Image();
                probe.onload = () => resolve();
                probe.onerror = () => resolve();
                probe.src = url;
            }))
        ));
        pendingBackgrounds = bgChecks.filter(c => !c.ok).length;
        pendingWaiting.push(...bgChecks.filter(c => !c.ok));
    }

    /* 3. Шрифты ----------------------------------------------------- */
    let pendingFonts = 0;
    if (document.fonts) {
        const ok = await withTimeout(document.fonts.ready);
        const status = document.fonts.status;
        if (!ok || status !== 'loaded') {
            pendingFonts = 1;
            pendingWaiting.push({ kind: 'font', url: 'document.fonts: ' + status });
        }
    }

    /* 4. Группируем незагруженное для лога -------------------------- */
    const grouped = new Map();
    for (const item of pendingWaiting) {
        const key = item.kind + '\u0000' + item.url;
        const entry = grouped.get(key) || { kind: item.kind, url: item.url, count: 0 };
        entry.count += 1;
        grouped.set(key, entry);
    }
    const pendingSamples = Array.from(grouped.values())
        .sort((a, b) => b.count - a.count)
        .slice(0, sampleLimit)
        .map(e => (e.count > 1
            ? `${e.kind} ×${e.count}: ${e.url}`
            : `${e.kind}: ${e.url}`));

    return {
        ready_state: document.readyState,
        images_total: images.length,
        images_waiting: waiting.length,
        pending_images: pendingImages,
        backgrounds_total: bgTotal,
        pending_backgrounds: pendingBackgrounds,
        pending_fonts: pendingFonts,
        pending_samples: pendingSamples,
    };
}
"""

# STORAGE_SCRIPT — проставляет localStorage ДО скриптов страницы, чтобы
# приложение сразу увидело нужные флаги (регион, согласия на cookie,
# отключённые A/B-эксперименты и т.п.). Пишем только в разрешённые
# origin'ы, чтобы не трогать сторонние iframe.
STORAGE_SCRIPT = r"""
(() => {
    const cfg = __STORAGE__;
    try {
        if (cfg.origins.length && !cfg.origins.includes(location.origin)) return;
        for (const key of Object.keys(cfg.data)) {
            window.localStorage.setItem(key, cfg.data[key]);
        }
    } catch (e) {}   // opaque origin / заблокированное хранилище
})();
"""


# ---------------------------------------------------------------- utilities

def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fmt_duration(seconds: float) -> str:
    """Компактно: 42с / 3м 07с / 1ч 02м 03с."""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}ч {minutes:02d}м {secs:02d}с"
    if minutes:
        return f"{minutes}м {secs:02d}с"
    return f"{secs}с"


def terminal_link(path: Path, label: str | None = None) -> str:
    """
    Делает путь кликабельным в терминале (OSC 8).
    VS Code и большинство современных терминалов открывают такую ссылку
    по Ctrl+Click (в VS Code — прямо в редакторе).

    Если вывод не в терминал (пайп, CI-лог), отдаёт обычный путь.
    """
    resolved = path.resolve()
    text = label or str(resolved)
    if not sys.stdout.isatty():
        return text
    return f"\x1b]8;;{resolved.as_uri()}\x1b\\{text}\x1b]8;;\x1b\\"


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


def freeze_options(cfg: dict) -> dict:
    """
    Нормализует настройки подготовки снимка (ожидание загрузки + заморозка).
    Все ключи необязательные, поэтому старые конфиги (где был только
    disable_animations) работают как раньше.
    """
    arm = str(cfg.get("arm_events", "freeze")).lower()
    if arm not in ("start", "freeze"):
        arm = "freeze"

    return {
        # --- ожидание реальной загрузки ресурсов ----------------------»
        "wait_for_media": bool(cfg.get("wait_for_media", True)),
        "media_timeout_ms": int(cfg.get("media_timeout_ms", 180_000)),
        "media_per_resource_ms": int(cfg.get("media_per_resource_ms", 30_000)),
        "wait_for_backgrounds": bool(cfg.get("wait_for_backgrounds", True)),
        # --- заморозка ------------------------------------------------
        # существующее поведение
        "disable_animations": bool(cfg.get("disable_animations", True)),
        # слушатели «шумных» событий (scroll, wheel, pointer, resize, ...)
        "events": bool(cfg.get("block_event_listeners", True)),
        "event_types": [str(t).lower() for t in
                        (cfg.get("blocked_events") or DEFAULT_EVENT_TYPES)],
        # start  — рубить регистрацию сразу (жёстче, но может съесть
        #          синтетические события, которыми сайт пересчитывает вёрстку);
        # freeze — рубить перед снимком (совместимо с lazy-load).
        "arm_events": arm,
        "strip_listeners": bool(cfg.get("strip_event_listeners", True)),
        # IntersectionObserver / ResizeObserver / MutationObserver
        "observers": bool(cfg.get("disable_observers", True)),
        # setTimeout / setInterval
        "timers": bool(cfg.get("disable_timers", False)),
        # requestAnimationFrame
        "raf": bool(cfg.get("disable_raf", True)),
        # Emulation.setScriptExecutionDisabled непосредственно перед снимком
        "kill_scripts": bool(cfg.get("kill_scripts_before_shot", True)),
    }


def freeze_summary(freeze: dict) -> str:
    parts: list[str] = []
    if freeze["wait_for_media"]:
        parts.append("ожидание ресурсов")
    if freeze["disable_animations"]:
        parts.append("анимации")
    if freeze["events"]:
        parts.append(f"слушатели (arm={freeze['arm_events']})")
    if freeze["observers"]:
        parts.append("observers")
    if freeze["raf"]:
        parts.append("rAF")
    if freeze["timers"]:
        parts.append("таймеры")
    if freeze["kill_scripts"]:
        parts.append("stop JS")
    return ", ".join(parts) or "выключена"


def build_guard_script(freeze: dict) -> str:
    """Инжектит опции в GUARD_SCRIPT для context.add_init_script()."""
    payload = json.dumps({
        "events": freeze["events"],
        "event_types": freeze["event_types"],
        "arm_events": freeze["arm_events"],
        "timers": freeze["timers"],
    })
    return GUARD_SCRIPT.replace("__OPTIONS__", payload)


def _storage_value(value: Any) -> str:
    """localStorage хранит только строки."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)   # dict/list → JSON-строка


def _apply_storage_placeholders(text: str, now_ms: int) -> str:
    """
    Подставляет текущее время в значение localStorage:

      $now_ms → миллисекунды (как Date.now())
      $now_s  → секунды

    Работает и в строковом JSON, и в разобранном из YAML объекте. Если
    токен обёрнут в кавычки ("$now_ms"), кавычки снимаются — в итоговом
    JSON должно оказаться число, а не строка.
    """
    now_s = now_ms // 1000
    for token, value in (("$now_ms", now_ms), ("$now_s", now_s)):
        text = text.replace(f'"{token}"', str(value))
        text = text.replace(token, str(value))
    return text


def storage_options(cfg: dict) -> tuple[dict[str, str], list[str]]:
    """
    Разбирает `local_storage` из конфига и определяет, каким origin'ам
    его проставлять.

    По умолчанию — только origin'ы эталона и стенда (из `reference`
    и `current`), чтобы не писать localStorage сторонним iframe'ам.
    Список можно задать явно через `local_storage_origins`.
    """
    raw = cfg.get("local_storage") or {}
    if not isinstance(raw, dict):
        return {}, []

    now_ms = int(time.time() * 1000)
    values = {
        str(k): _apply_storage_placeholders(_storage_value(v), now_ms)
        for k, v in raw.items()
    }

    origins = cfg.get("local_storage_origins")
    if origins is not None:
        return values, [str(o) for o in origins]

    origins = []
    for base in (cfg.get("reference"), cfg.get("current")):
        if not base:
            continue
        parts = urlsplit(str(base))
        origin = f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""
        if origin and origin not in origins:
            origins.append(origin)
    return values, origins


def build_storage_script(cfg: dict) -> str:
    """Скрипт для context.add_init_script() или пустая строка."""
    values, origins = storage_options(cfg)
    if not values:
        return ""
    payload = json.dumps(
        {"data": values, "origins": origins}, ensure_ascii=False
    )
    return STORAGE_SCRIPT.replace("__STORAGE__", payload)


def _registrable_domain(host: str) -> str:
    """
    Грубый eTLD+1: googletagmanager.com, sberdevices.ru, localhost.
    Нужен, чтобы cdn.sberdevices.ru и pr-1.dev.sberdevices.ru считались
    «своими» для сайта на sberdevices.ru.

    IP-адреса возвращаются как есть: 127.0.0.1 — это не «домен 0.1».
    """
    host = (host or "").lower().strip(".")
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last_two = ".".join(labels[-2:])
    if last_two in MULTI_LABEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return last_two


def _host_of(value: str) -> str:
    """Принимает и URL, и просто хост."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "//" in text:
        return (urlsplit(text).hostname or "").lower()
    return text.lower().strip("/")


def third_party_options(cfg: dict) -> dict:
    """
    Настройки блокировки сторонних запросов.

    Своими считаются домены из `reference`/`current` (по eTLD+1) —
    то есть сам сайт и его поддомены/CDN. Всё остальное для выбранных
    типов ресурсов не загружается: без GTM/метрик/чатов снимок
    становится заметно стабильнее, и в кадр не лезут чужие iframe'ы.
    """
    raw = cfg.get("block_third_party")
    if isinstance(raw, dict):
        enabled = bool(raw.get("enabled", True))
        types = raw.get("resource_types") or DEFAULT_BLOCKED_RESOURCE_TYPES
        allow = raw.get("allow") or []
        log = bool(raw.get("log", True))
    else:
        enabled = True if raw is None else bool(raw)
        types = DEFAULT_BLOCKED_RESOURCE_TYPES
        allow = []
        log = True

    domains: set[str] = set()
    for base in (cfg.get("reference"), cfg.get("current")):
        host = _host_of(base)
        if host:
            domains.add(_registrable_domain(host))

    allow_hosts = {h for h in (_host_of(a) for a in allow) if h}

    return {
        "enabled": enabled,
        "resource_types": {str(t).lower() for t in types},
        "allowed_domains": domains,
        "allow_hosts": allow_hosts,
        "log": log,
    }


async def apply_third_party_block(context: BrowserContext, opts: dict) -> Counter:
    """
    Рубит запросы к чужим доменам для выбранных типов ресурсов.
    Возвращает счётчик заблокированных хостов (может дописываться по ходу).

    Регистрировать надо РАНЬШЕ моков: Playwright проверяет обработчики
    от последнего к первому, поэтому моки должны иметь приоритет.
    """
    blocked: Counter = Counter()
    if not opts["enabled"]:
        return blocked

    types = opts["resource_types"]
    allowed = opts["allowed_domains"]
    extra = opts["allow_hosts"]

    async def handler(route: Route) -> None:
        request = route.request
        kind = request.resource_type

        # iframe Playwright тоже называет `document` — отличаем по фрейму
        if kind == "document":
            try:
                frame = request.frame
            except Exception:
                frame = None
            if frame is None or frame.parent_frame is None:
                await route.continue_()      # главный документ не трогаем
                return
            kind = "subdocument"

        if kind in types:
            host = (urlsplit(request.url).hostname or "").lower()
            if host and host not in extra and _registrable_domain(host) not in allowed:
                blocked[host] += 1
                await route.abort()
                return

        await route.continue_()

    await context.route("**/*", handler)
    return blocked


def format_blocked(blocked: Counter, limit: int = 5) -> str:
    """«googletagmanager.com ×2, mc.yandex.ru» — для лога."""
    if not blocked:
        return ""
    parts = []
    for host, count in blocked.most_common(limit):
        parts.append(f"{host} ×{count}" if count > 1 else host)
    if len(blocked) > limit:
        parts.append(f"и ещё {len(blocked) - limit}")
    return ", ".join(parts)


async def strip_existing_listeners(cdp: CDPSession, event_types: list[str]) -> int:
    """
    Снимает уже зарегистрированные слушатели по «шумным» событиям
    с window/document/html/body. Работает через DevTools-API
    getEventListeners (includeCommandLineAPI).
    """
    expression = f"({STRIP_LISTENERS_JS})({json.dumps(event_types)})"
    try:
        result = await cdp.send("Runtime.evaluate", {
            "expression": expression,
            "includeCommandLineAPI": True,
            "returnByValue": True,
        })
    except Exception as e:
        print(f"  ! strip listeners failed: {e}", file=sys.stderr)
        return 0

    removed = (result.get("result") or {}).get("value")
    if isinstance(removed, int) and removed > 0:
        print(f"  · снято слушателей: {removed}")
        return removed
    return 0


async def wait_for_media(page: Page, freeze: dict, label: str = "") -> dict:
    """
    Ждёт реальной готовности страницы к снимку, а не фиксированную паузу:
    картинки загружены и раскодированы (img.decode), CSS-фоны подтянуты,
    шрифты применены. Один раз доскролливает страницу, чтобы разбудить
    «ленивые» элементы, которые не захотели запрашивать ресурс сразу.

    Возвращает последнюю статистику. При исчерпании media_timeout_ms
    просто отпускает страницу — снимок важнее ожидания.
    """
    timeout_ms = int(freeze["media_timeout_ms"])
    per_resource_ms = int(freeze["media_per_resource_ms"])
    deadline = time.monotonic() + timeout_ms / 1000 if timeout_ms > 0 else None
    prefix = f"[{label}] " if label else ""

    stats: dict[str, Any] = {}
    stable = 0
    rescrolled = False
    rounds = 0
    timed_out = False

    while True:
        rounds += 1

        # бюджет на ресурс не может перебивать остаток общего таймаута,
        # иначе одна итерация легко съедает весь media_timeout_ms
        budget_ms = per_resource_ms
        if deadline is not None:
            left_ms = int((deadline - time.monotonic()) * 1000)
            budget_ms = min(per_resource_ms, max(left_ms, 1000))

        opts = {
            "per_resource_ms": budget_ms,
            "backgrounds": freeze["wait_for_backgrounds"],
            "sample_limit": 15,
        }
        try:
            stats = await page.evaluate(WAIT_MEDIA_JS, opts)
        except Exception as e:
            print(f"  ! {prefix}wait_media failed: {e}", file=sys.stderr)
            return stats

        pending = (
            int(stats.get("pending_images", 0))
            + int(stats.get("pending_backgrounds", 0))
            + int(stats.get("pending_fonts", 0))
        )

        if pending == 0:
            stable += 1
            # две подряд «чистые» итерации — значит ничего не догружается
            if stable >= 2:
                break
        else:
            stable = 0
            if not rescrolled:
                try:
                    await page.evaluate(SCROLL_JS)
                except Exception as e:
                    print(f"  ! rescan scroll failed: {e}", file=sys.stderr)
                rescrolled = True

        if deadline is not None and time.monotonic() > deadline:
            timed_out = True
            break

        await page.wait_for_timeout(300)

    if timed_out:
        print(f"  ! {prefix}wait_media: таймаут {fmt_duration(timeout_ms / 1000)} — "
              f"не дождались: img {stats.get('pending_images', 0)}, "
              f"bg {stats.get('pending_backgrounds', 0)}, "
              f"font {stats.get('pending_fonts', 0)}", file=sys.stderr)
        for sample in stats.get("pending_samples", []):
            print(f"      {sample}", file=sys.stderr)
    else:
        print(f"  · ресурсы готовы ({rounds} итерац.): "
              f"img {stats.get('images_waiting')}/{stats.get('images_total')}, "
              f"фонов {stats.get('backgrounds_total', 0)}")

    return stats


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
    freeze: dict,
    side: str = "",
    storage_script: str = "",
    third_party: dict | None = None,
) -> None:
    animate = freeze["disable_animations"]
    prefix_log = f"[{side}] " if side else ""

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
        # чужие скрипты/iframe'ы отсекаем ДО моков: у моков приоритет
        blocked = await apply_third_party_block(
            context, third_party or {"enabled": False}
        )
        await apply_mocks(context, mocks)

        # предохранитель ставится ДО скриптов страницы, поэтому он
        # перехватывает вообще все регистрации слушателей
        await context.add_init_script(script=build_guard_script(freeze))

        # localStorage — тоже до скриптов страницы (регион, согласия, флаги A/B)
        if storage_script:
            await context.add_init_script(script=storage_script)

        page = await context.new_page()
        target = url_join(base_url, rel_url)

        cdp: CDPSession | None = None
        if freeze["strip_listeners"] or freeze["kill_scripts"] or animate:
            try:
                cdp = await context.new_cdp_session(page)
            except Exception as e:
                print(f"  ! cdp session failed: {e}", file=sys.stderr)

        try:
            await page.goto(target, wait_until="load", timeout=60_000)
        except Exception as e:
            print(f"  ! goto failed {target}: {e}", file=sys.stderr)

        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        # первая заморозка — до прогрева (стабилизирует анимации и маски)
        await freeze_page(page, mask_selectors, animate)

        # прокрутка для lazy-load
        try:
            await page.evaluate(SCROLL_JS)
        except Exception as e:
            print(f"  ! scroll failed: {e}", file=sys.stderr)

        # ждём, пока все ресурсы действительно прогрузятся и раскодируются
        if freeze["wait_for_media"]:
            await wait_for_media(page, freeze, side)

        # settle_ms теперь — именно ДОБАВКА после загрузки ресурсов
        # (отложенные блоки, пересчёт layout), а не основное ожидание
        if settle_ms:
            await page.wait_for_timeout(settle_ms)

        # вторая заморозка — scroll мог оживить анимации
        await freeze_page(page, mask_selectors, animate)

        # снимаем уже навешанные слушатели по «шумным» событиям
        if freeze["strip_listeners"] and cdp is not None:
            await strip_existing_listeners(cdp, freeze["event_types"])

        # жёсткая заморозка: слушатели, observers, таймеры, rAF
        try:
            await page.evaluate(HARD_FREEZE_JS, freeze)
        except Exception as e:
            print(f"  ! hard_freeze failed: {e}", file=sys.stderr)

        # финальный «стоп-кран»: видео и CSS/WAAPI-анимации
        if animate:
            try:
                await page.evaluate(FINAL_FREEZE_JS)
            except Exception as e:
                print(f"  ! final_freeze failed: {e}", file=sys.stderr)

        # серверная заморозка: playbackRate=0 и полная остановка JS
        if cdp is not None:
            if animate:
                try:
                    await cdp.send("Animation.enable")
                    await cdp.send("Animation.setPlaybackRate", {"playbackRate": 0})
                except Exception as e:
                    print(f"  ! animation freeze failed: {e}", file=sys.stderr)
            if freeze["kill_scripts"]:
                try:
                    await cdp.send(
                        "Emulation.setScriptExecutionDisabled", {"value": True}
                    )
                except Exception as e:
                    print(f"  ! kill_scripts failed: {e}", file=sys.stderr)

        # дать текущему кадру завершиться
        await page.wait_for_timeout(300)

        try:
            await page.screenshot(path=str(out_path), full_page=True)
        except Exception as e:
            # если остановленный JS помешал full_page-снимку — повторяем
            # попытку с включённым движком
            print(f"  ! screenshot failed ({e}), retry with JS enabled",
                  file=sys.stderr)
            if cdp is not None and freeze["kill_scripts"]:
                try:
                    await cdp.send(
                        "Emulation.setScriptExecutionDisabled", {"value": False}
                    )
                except Exception:
                    pass
            await page.screenshot(path=str(out_path), full_page=True)

        if third_party and third_party.get("log") and blocked:
            print(f"  · {prefix_log}заблокировано сторонних запросов: "
                  f"{format_blocked(blocked)}")

        await browser.close()


# --------------------------------------------------------------- main loop

async def check_one(
    rel_url: str,
    vp_name: str,
    viewport: dict,
    cfg: dict,
    outdir: Path,
    index: int = 1,
    total: int = 1,
    elapsed_s: float = 0.0,
) -> dict[str, Any]:
    name = slug(rel_url)
    ref_png = outdir / f"{name}__{vp_name}__ref.png"
    cur_png = outdir / f"{name}__{vp_name}__cur.png"
    diff_png = outdir / f"{name}__{vp_name}__diff.png"

    pct = index * 100 / total if total else 100.0
    started = time.monotonic()
    print(f"[{index}/{total} · {pct:.0f}% · прошло {fmt_duration(elapsed_s)}] "
          f"→ [{vp_name}] {rel_url}", flush=True)

    freeze = freeze_options(cfg)
    mocks = cfg.get("mocks", {})
    masks = cfg.get("mask_selectors", [])
    settle_ms = cfg.get("settle_ms", 1500)

    storage_script = build_storage_script(cfg)
    third_party = third_party_options(cfg)

    await take_screenshot(
        cfg["reference"], rel_url, viewport, ref_png,
        mocks, settle_ms, masks, freeze, side="эталон",
        storage_script=storage_script, third_party=third_party,
    )
    await take_screenshot(
        cfg["current"], rel_url, viewport, cur_png,
        mocks, settle_ms, masks, freeze, side="стенд",
        storage_script=storage_script, third_party=third_party,
    )

    ratio = diff_images(ref_png, cur_png, diff_png)
    status = "ok" if ratio <= cfg["diff_threshold"] else "diff"
    print(f"   {'✓' if status == 'ok' else '✗'} diff={ratio:.4%} "
          f"· {fmt_duration(time.monotonic() - started)}", flush=True)

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
  .pair{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;position:absolute;left:0;padding:24px;background:#333;z-index:1;}
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
     Расхождений: {{ broken }} ·
     Заморозка: {{ freeze_summary }}</div>

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
    freeze = freeze_options(cfg)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = Path("reports") / ts
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"Заморозка: {freeze_summary(freeze)}")

    storage, storage_origins = storage_options(cfg)
    if storage:
        print(f"localStorage: {', '.join(storage)} → "
              f"{', '.join(storage_origins) or 'все origin'}")

    third_party = third_party_options(cfg)
    if third_party["enabled"]:
        print(f"Сторонние: блокирую {'/'.join(sorted(third_party['resource_types']))} "
              f"вне {', '.join(sorted(third_party['allowed_domains'])) or '—'}")

    results: list[dict[str, Any]] = []

    total = len(cfg["urls"]) * len(cfg["viewports"])
    done = 0
    run_started = time.monotonic()

    for rel_url in cfg["urls"]:
        for vp_name, vp in cfg["viewports"].items():
            done += 1
            try:
                results.append(
                    await check_one(rel_url, vp_name, vp, cfg, outdir,
                                    index=done, total=total,
                                    elapsed_s=time.monotonic() - run_started)
                )
            except Exception as e:
                print(f"   ! FAILED {rel_url} [{vp_name}] "
                      f"(прошло {fmt_duration(time.monotonic() - run_started)}): {e}",
                      file=sys.stderr, flush=True)
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
        freeze_summary=freeze_summary(freeze),
    )
    report = outdir / "index.html"
    report.write_text(html, encoding="utf-8")

    # подпись — путь относительно cwd: коротко и его же подхватывает
    # встроенное распознавание ссылок терминала, если OSC 8 не поддержан
    resolved = report.resolve()
    try:
        label = str(resolved.relative_to(Path.cwd()))
    except ValueError:
        label = str(resolved)

    print(f"\nОтчёт: {terminal_link(resolved, label)}")
    print(f"Проверок: {len(results)}/{total}, расхождений: {broken} · "
          f"за {fmt_duration(time.monotonic() - run_started)}")
    return 0 if broken == 0 else 1


if __name__ == "__main__":
    cfg = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    sys.exit(asyncio.run(main(cfg)))