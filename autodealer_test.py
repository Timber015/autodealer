[autodealer_test.py](https://github.com/user-attachments/files/26841555/autodealer_test.py)
"""
Тест логина в АвтоДилер Онлайн через Playwright.
Логинится, открывает страницу нового заказ-наряда, делает скриншоты.

Запуск: python3 autodealer_test.py

Перед запуском в /root/.bot_env должны быть:
    AUTODEALER_LOGIN=ваш_логин
    AUTODEALER_PASSWORD=ваш_пароль
"""

import asyncio
import os
import sys
from playwright.async_api import async_playwright

URL_LOGIN = "https://online.autodealer.ru/"
URL_NEW_ORDER = "https://online.autodealer.ru/documents/order/new/4"

LOGIN = os.environ.get("AUTODEALER_LOGIN", "")
PASSWORD = os.environ.get("AUTODEALER_PASSWORD", "")

# Селекторы — пробуем разные варианты, подстроимся после первого прогона
LOGIN_SELECTORS = [
    'input[name="login"]',
    'input[name="username"]',
    'input[name="email"]',
    'input[type="text"]',
    'input[type="email"]',
]

PASSWORD_SELECTORS = [
    'input[name="password"]',
    'input[type="password"]',
]

SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Войти")',
    'button:has-text("Вход")',
]


async def try_fill(page, selectors: list, value: str, label: str):
    """Пробует несколько селекторов, заполняет первый найденный."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.fill(value)
                print(f"  ✅ {label}: заполнено через '{sel}'")
                return sel
        except Exception as e:
            continue
    print(f"  ❌ {label}: не нашёл поле ни по одному селектору")
    return None


async def try_click(page, selectors: list, label: str):
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click()
                print(f"  ✅ {label}: клик через '{sel}'")
                return sel
        except Exception as e:
            continue
    print(f"  ❌ {label}: не нашёл кнопку")
    return None


async def main():
    if not LOGIN or not PASSWORD:
        print("❌ Нет AUTODEALER_LOGIN или AUTODEALER_PASSWORD в переменных окружения")
        print("   Добавь их в /root/.bot_env и выполни: source /root/.bot_env")
        sys.exit(1)

    print(f"→ Логин: {LOGIN[:3]}***{LOGIN[-3:] if len(LOGIN) > 6 else ''}")
    print(f"→ Пароль: ***{'*' * len(PASSWORD)}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="ru-RU")
        page = await context.new_page()

        # === ШАГ 1 — открываем страницу логина ===
        print("\n[1/4] Открываю страницу логина...")
        await page.goto(URL_LOGIN, wait_until="networkidle", timeout=30000)
        await page.screenshot(path="/root/ad_1_login_page.png", full_page=True)
        print(f"  URL: {page.url}")
        print(f"  Title: {await page.title()}")
        print("  → скриншот: /root/ad_1_login_page.png")

        # === ШАГ 2 — заполняем форму ===
        print("\n[2/4] Заполняю форму логина...")
        await try_fill(page, LOGIN_SELECTORS, LOGIN, "Логин")
        await try_fill(page, PASSWORD_SELECTORS, PASSWORD, "Пароль")
        await page.screenshot(path="/root/ad_2_filled.png", full_page=True)
        print("  → скриншот: /root/ad_2_filled.png")

        # === ШАГ 3 — отправляем форму ===
        print("\n[3/4] Отправляю форму...")
        await try_click(page, SUBMIT_SELECTORS, "Кнопка входа")
        await page.wait_for_load_state("networkidle", timeout=30000)
        await page.screenshot(path="/root/ad_3_after_login.png", full_page=True)
        print(f"  URL после логина: {page.url}")
        print("  → скриншот: /root/ad_3_after_login.png")

        # === ШАГ 4 — открываем форму нового наряда ===
        print("\n[4/4] Открываю форму нового заказ-наряда...")
        try:
            await page.goto(URL_NEW_ORDER, wait_until="networkidle", timeout=30000)
            await page.screenshot(path="/root/ad_4_new_order.png", full_page=True)
            print(f"  URL: {page.url}")
            print(f"  Title: {await page.title()}")
            print("  → скриншот: /root/ad_4_new_order.png")

            # Сохраняем HTML формы для анализа селекторов
            html = await page.content()
            with open("/root/ad_form.html", "w", encoding="utf-8") as f:
                f.write(html)
            print("  → HTML формы: /root/ad_form.html")
        except Exception as e:
            print(f"  ❌ Ошибка при открытии формы: {e}")

        print("\n✅ Тест завершён. Скриншоты в /root/ad_*.png, HTML в /root/ad_form.html")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
