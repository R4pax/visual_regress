# Visual Regression — проверка визуальных изменений сайта

Скрипт сравнивает скриншоты эталонного сайта и сайта с изменениями
в десктопном (1440px) и мобильном (360px) разрешениях. Поддерживает
мокирование бэкенд-запросов, чтобы состояния страниц были
детерминированными. Результат — HTML-отчёт с расхождениями и
скриншотами.

---

## Работа с проектом

### Войти в проект

```bash
cd visual_regress
source .venv/bin/activate
```

После этого в приглашении появится префикс `(.venv)`:

```
(.venv) user@host:~/visual_regress$
```

Проверка, что активен именно venv:

```bash
which python     # должен показать .venv/bin/python
which pip        # должен показать .venv/bin/pip
```

Для других оболочек:

- fish: `source .venv/bin/activate.fish`
- PowerShell: `.venv\Scripts\Activate.ps1`

### Запустить проверку

```bash
# базовый запуск с config.yaml
python run_check.py

# или с другим конфигом
python run_check.py config.staging.yaml
```

Скрипт создаст папку `reports/<timestamp>/` с HTML-отчётом:

```
reports/20260115_143022/
├── index.html                    ← открыть в браузере
├── home__desktop__ref.png
├── home__desktop__cur.png
├── home__desktop__diff.png
└── ...
```

Открыть отчёт:

```bash
xdg-open reports/*/index.html      # Linux
open reports/*/index.html          # macOS
```

В конце прогона скрипт печатает путь к отчёту кликабельной ссылкой
(OSC 8): в VS Code и большинстве современных терминалов она открывается
по Ctrl+Click. Подпись — путь относительно текущей папки, поэтому
работает и обычное распознавание ссылок терминалом. Если вывод уходит
в пайп/CI-лог, OSC 8 не добавляется — печатается просто путь.

Прогресс виден в логах: перед каждой проверкой печатается
`[N/M · P% · прошло <время>]`, после — результат и время, затраченное
на эту страницу:

```
[1/132 · 1% · прошло 0с] → [desktop] /
  · ресурсы готовы (2 итерац.): img 3/3, фонов 1
  ✓ diff=0.0000% · 4с
```

Время форматируется компактно: `42с` → `3м 07с` → `1ч 02м 03с`.
Суммарное время прогона печатается в конце, вместе со счётчиком проверок.

Код возврата:

- `0` — расхождений нет
- `1` — есть расхождения (удобно для CI)

### Выйти из проекта

```bash
deactivate
```

Префикс `(.venv)` исчезнет, ты снова в системном Python.

---

## Ожидание загрузки ресурсов

Раньше снимок делался после фиксированной паузы `settle_ms` — из-за этого
в кадр часто попадали картинки, которые успели загрузиться лишь наполовину.
Теперь основной механизм — ожидание реальной готовности (`wait_for_media`):

1. **Прогрев.** Страница прокручивается целиком, чтобы разбудить
   `loading="lazy"`, карусели и блоки, появляющиеся по скроллу.
2. **Ожидание картинок.** Для каждого видимого `<img>` (включая same-origin
   iframe) ждётся `load`, а затем `img.decode()`. Именно `decode()` закрывает
   кейс progressive JPEG: `img.complete === true` уже давно, а в кадр
   попадает половина картинки.
3. **CSS-фоны.** Из computed-стилей собираются все `background-image: url(...)`
   и предзагружаются через `new Image()` — фон часто и есть та самая
   «недогруженная» картинка.
4. **Шрифты.** Ждётся `document.fonts.ready`.
5. **Стабилизация.** Ожидание заканчивается после двух «чистых» итераций
   подряд: все ресурсы готовы и новых не появляется. Если что-то не
   догрузилось — один раз делается контрольная прокрутка.

Незагруженные (но видимые) ресурсы ждутся не бесконечно: на каждый ресурс
есть потолок `media_per_resource_ms`, на всю страницу — `media_timeout_ms`.
После таймаута снимок всё равно делается, в stderr пишется предупреждение.

Скрытые элементы (`display: none` / `visibility: hidden`) пропускаются —
это и ускоряет ожидание, и не заставляет ждать анимированные GIF,
которые всё равно вырезаны заморозкой.

`settle_ms` никуда не делся, но менял смысл: теперь это **добавка** после
загрузки ресурсов (страховка для блоков, которые появляются через JS
с задержкой), а не основное ожидание.

### Ключи конфига ожидания

| Ключ                    | По умолчанию | Что делает                                         |
| ----------------------- | ------------ | -------------------------------------------------- |
| `wait_for_media`        | `true`       | Ждать реальной загрузки ресурсов                   |
| `media_timeout_ms`      | `180000`     | Общий бюджет ожидания на страницу (0 — без лимита) |
| `media_per_resource_ms` | `30000`      | Потолок ожидания на один ресурс                    |
| `wait_for_backgrounds`  | `true`       | Догружать CSS-фоны (`background-image`)            |
| `settle_ms`             | `1500`       | Добавочная пауза ПОСЛЕ загрузки ресурсов           |

---

## Заморозка страницы

Даже с `disable_animations: true` страница может «дёрнуться» после
прокрутки: сработает скролл-хендлер, `IntersectionObserver`, `setTimeout`
или rAF-цикл. Для этого есть отдельный слой заморозки — четыре ступени:

1. **Предохранитель слушателей.** Через `context.add_init_script` (до
   скриптов страницы) перехватывается `EventTarget.prototype.addEventListener`.
   Пока предохранитель не взведён, всё работает как обычно — поэтому
   ленивая загрузка по скроллу успевает отработать.
2. **Снятие живых слушателей.** Перед снимком уже навешанные слушатели по
   «шумным» событиям удаляются с `window`/`document`/`html`/`body` через
   DevTools-API `getEventListeners` (CDP `Runtime.evaluate` с
   `includeCommandLineAPI`).
3. **Гашение observers и таймеров.** Живые `IntersectionObserver`,
   `ResizeObserver`, `MutationObserver` отключаются, конструкторы
   подменяются заглушками; `requestAnimationFrame` перестаёт планировать
   кадры. `setTimeout`/`setInterval` гасятся опционально.
4. **Остановка JS.** Последним шагом вызывается CDP
   `Emulation.setScriptExecutionDisabled` — после снимка в странице вообще
   ничего не исполняется. Если full-page-скриншот на этом споткнётся,
   скрипт автоматически повторит попытку с включённым JS.

Какие события глушатся по умолчанию (см. `DEFAULT_EVENT_TYPES`):
`scroll`, `wheel`, `touchstart/move/end`, `pointermove/down/up`,
`mousemove/down/up/over/out`, `keydown/up/press`, `resize`,
`orientationchange`, `drag*`.

Сознательно **не** глушатся `load`, `DOMContentLoaded`, `animationend`,
`transitionend`: часть библиотек ждёт их для инициализации.

### Ключи конфига

| Ключ                       | По умолчанию | Что делает                                         |
| -------------------------- | ------------ | -------------------------------------------------- |
| `wait_for_media`           | `true`       | Ждать реальной загрузки картинок/фонов/шрифтов     |
| `media_timeout_ms`         | `180000`     | Общий бюджет ожидания на страницу                  |
| `media_per_resource_ms`    | `30000`      | Потолок ожидания на один ресурс                    |
| `wait_for_backgrounds`     | `true`       | Догружать CSS-фоны (`background-image`)            |
| `disable_animations`       | `true`       | CSS/WAAPI-анимации, видео, GIF/APNG                |
| `block_event_listeners`    | `true`       | Глушить слушатели «шумных» событий                 |
| `blocked_events`           | —            | Свой список событий (заменяет дефолтный)           |
| `arm_events`               | `freeze`     | `freeze` — взводить перед снимком, `start` — сразу |
| `strip_event_listeners`    | `true`       | Снять уже навешанные слушатели                     |
| `disable_observers`        | `true`       | Гасить Intersection/Resize/MutationObserver        |
| `disable_raf`              | `true`       | Гасить `requestAnimationFrame`                     |
| `disable_timers`           | `false`      | Гасить `setTimeout`/`setInterval` (агрессивно)     |
| `kill_scripts_before_shot` | `true`       | Выключить JS-движок перед снимком                  |

Пример «максимальной» заморозки:

```yaml
disable_animations: true
block_event_listeners: true
arm_events: start
strip_event_listeners: true
disable_observers: true
disable_raf: true
disable_timers: true
kill_scripts_before_shot: true
```

Если начали появляться ложные расхождения — сначала верни
`arm_events: freeze` (блокировка с самого старта может съесть синтетические
события, которыми сайт пересчитывает вёрстку), затем `disable_timers: false`.

---

## Требования

- Linux (Debian/Ubuntu) или macOS
- Python 3.12+ (проверено на 3.14)
- ~500 MB свободного места (venv + Chromium)

## Первичная установка

```bash
# 1. Системные зависимости
sudo apt update
sudo apt install -y python3-venv python3-full \
                    libjpeg-dev zlib1g-dev libtiff-dev \
                    libfreetype6-dev libwebp-dev

# 2. Виртуальное окружение
cd visual_regress
python3 -m venv .venv
source .venv/bin/activate

# 3. Обновление pip
pip install --upgrade pip setuptools wheel

# 4. Python-зависимости
pip install -r requirements.txt

# 5. Браузер для Playwright
playwright install chromium
playwright install-deps chromium

# 6. Зафиксировать версии
pip freeze > requirements.lock.txt
```

## Проверка установки

```bash
python -c "import PIL; print('Pillow', PIL.__version__)"
python -c "import playwright; print('Playwright OK')"
python -c "from greenlet import greenlet; print('greenlet OK')"
```

Ожидаемый результат:

```
Pillow 12.3.0
Playwright OK
greenlet OK
```

---

## requirements.txt

```txt
playwright>=1.51.0
pixelmatch==0.3.0
Pillow>=11.0.0
PyYAML==6.0.2
Jinja2==3.1.4
```

Версии подобраны под Python 3.14. Для более старых Python (3.10–3.12)
можно использовать фиксированные версии:

```txt
playwright==1.47.0
pixelmatch==0.3.0
Pillow==10.4.0
PyYAML==6.0.2
Jinja2==3.1.4
```

---

## Решение проблем

### `error: externally-managed-environment`

Debian/Ubuntu блокируют установку пакетов в системный Python (PEP 668).
Решение — использовать venv. **Не используйте** `--break-system-packages`.

### `The headers or library files could not be found for jpeg`

Pillow собирается из исходников и не находит libjpeg:

```bash
sudo apt install -y libjpeg-dev zlib1g-dev libtiff-dev \
                    libfreetype6-dev libwebp-dev
```

### `greenlet` не собирается на Python 3.14

Версия greenlet из старых Playwright не поддерживает внутренние
структуры CPython 3.14 (`_PyCFrame`, `_PyInterpreterFrame`).
Обновите Playwright до >=1.51.0 — greenlet подтянется совместимый.

### `playwright: command not found`

Команда доступна только внутри активированного venv:

```bash
source .venv/bin/activate
playwright install chromium
```

### Ошибки про системные библиотеки для Chromium

```bash
playwright install-deps chromium
```

### Если ничего не помогает — Python 3.12

Более предсказуемая база для Playwright/Pillow:

```bash
sudo apt install -y python3.12 python3.12-venv
deactivate 2>/dev/null
rm -rf .venv
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
```

---

## Структура проекта

```
visual_regress/
├── .venv/                    # виртуальное окружение (не коммитить)
├── config.yaml               # URL-ы, viewport-ы, пороги, моки
├── mocks/
│   ├── basket.json
│   └── lk.json
├── run_check.py              # основной скрипт
├── requirements.txt
├── requirements.lock.txt     # зафиксированные версии (pip freeze)
├── reports/                  # отчёты (не коммитить)
└── README.md
```

## .gitignore

```
.venv/
reports/
__pycache__/
*.pyc
```
