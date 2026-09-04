"""
Web Scraper - Monitor cen i dostępności produktów (x-kom optimized)
"""

import os
import time
import re
from datetime import datetime, timedelta
import pandas as pd
from playwright.sync_api import sync_playwright

# =============================================================================
# KONFIGURACJA
# =============================================================================

INPUT_FILE = "urls.txt"
OUTPUT_FILE = "products_data.csv"
HEADLESS = True

# Delay - pierwszy request (cookies) vs kolejne
DELAY_FIRST_REQUEST = 10  # Dłuższy na początku (cookies, render)
DELAY_BETWEEN_REQUESTS = 5  # Krótszy dla kolejnych (już zaakceptowane cookies)

# TRYB BACKFILL - uzupełnianie brakujących dni
BACKFILL_MODE = False # Ustaw True gdy chcesz uzupełnić brakujące dni
BACKFILL_DATES = ["2026-08-12","2026-08-13"]  # Daty do uzupełnienia

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# =============================================================================
# KATEGORIE PRODUKTÓW
# =============================================================================

PRODUCT_CATEGORIES = {
    "Karta graficzna": [
        "karta graficzna",
        "geforce",
        "rtx",
        "gtx",
        "radeon",
        "rx ",
        "nvidia",
        "amd radeon",
        "gpu"
    ],
    "Płyta główna": [
        "płyta główna",
        "motherboard",
        "mainboard",
        # Chipsety AMD
        "b650", "x670", "b850", "x870", "a620",
        "b550", "x570", "a520", "b450", "x470",
        # Chipsety Intel
        "z790", "b760", "h770", "z690", "b660", "h670",
        "z590", "b560", "h510", "z490", "b460",
        # Producenci + model (dla pewności)
        "aorus", "tuf gaming", "rog strix",
        # Socket keywords
        "socket am5", "socket am4", "socket lga"
    ],
    "Procesor": [
        "procesor",
        "intel core",
        "amd ryzen",
        "cpu",
        "i3-", "i5-", "i7-", "i9-",
        "ryzen 3", "ryzen 5", "ryzen 7", "ryzen 9",
        "threadripper",
        "xeon"
    ],
    "Pamięć RAM": [
        "pamięć ram",
        "memoria ram",
        "ddr4",
        "ddr5",
        "ram ",
        "dimm",
        "sodimm"
    ],
    "Dysk": [
        # SSD
        "dysk ssd",
        "ssd m.2",
        "nvme",
        "sata ssd",
        "pcie gen4",
        "pcie gen5",
        "nm790",  # Modele
        "9100 pro",
        "sd810",
        # HDD
        "dysk hdd",
        "barracuda",
        "hat3320",
        "7200obr",
        # Ogólne (sprawdzane w URL)
        "/dysk-ssd",
        "/dysk-hdd",
        "/dysk-zewnetrzny"
    ],
    "Zasilacz": [
        "zasilacz",
        "psu",
        "power supply",
        "600w", "650w", "750w", "850w", "1000w"
    ],
    "Obudowa": [
        "obudowa",
        "case pc",
        "midi tower",
        "full tower"
    ]
}

# =============================================================================
# FUNKCJE POMOCNICZE
# =============================================================================

def load_urls():
    """Wczytuje listę URL-i z pliku"""
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        return [x.strip() for x in f if x.strip()]


def detect_category(product_name, url=""):
    """
    Wykrywa kategorię produktu na podstawie nazwy i URL
    
    Args:
        product_name: Nazwa produktu (np. "GeForce RTX 4090")
        url: URL produktu (opcjonalnie, dla dodatkowego kontekstu)
    
    Returns:
        str: Nazwa kategorii lub "Inne"
    """
    text_to_check = f"{product_name} {url}".lower()
    
    # =========================================================================
    # PRIORYTET 1: URL (najbardziej wiarygodne źródło)
    # =========================================================================
    
    # Dyski
    if any(x in text_to_check for x in ["/dysk-ssd", "/dysk-hdd", "/dysk-zewnetrzny"]):
        return "Dysk"
    
    # Płyty główne
    if any(x in text_to_check for x in ["/plyta-glowna", "/plyta-główna", "socket-am5", "socket-am4", "socket-lga"]):
        return "Płyta główna"
    
    # Procesory
    if "/procesor" in text_to_check:
        return "Procesor"
    
    # Karty graficzne
    if any(x in text_to_check for x in ["/karta-graficzna", "/karta-vga"]):
        return "Karta graficzna"
    
    # Pamięć RAM
    if "/pamiec-ram" in text_to_check:
        return "Pamięć RAM"
    
    # Zasilacze
    if "/zasilacz" in text_to_check:
        return "Zasilacz"
    
    # Obudowy
    if "/obudowa" in text_to_check:
        return "Obudowa"
    
    # =========================================================================
    # PRIORYTET 2: Słowa kluczowe (jeśli URL nie dał odpowiedzi)
    # =========================================================================
    
    for category, keywords in PRODUCT_CATEGORIES.items():
        for keyword in keywords:
            if keyword.lower() in text_to_check:
                return category
    
    return "Inne"


def accept_cookies(page):
    """
    Próbuje zaakceptować cookies (polskie warianty)
    Zwraca True jeśli coś kliknęło, False jeśli nie było cookies
    """
    cookie_buttons = [
        "button:has-text('W porządku')",
        "button:has-text('Akceptuj wszystko')",
        "button:has-text('Akceptuję')",
        "button:has-text('OK')",
    ]
    
    for button_selector in cookie_buttons:
        try:
            page.locator(button_selector).click(timeout=3000)
            print("  🍪 Zaakceptowano cookies")
            return True  # Znaleziono i kliknięto
        except:
            continue
    
    return False  # Nie było cookies do kliknięcia


def clean_price(raw_price):
    """
    Wyciąga samą liczbę z ciągu znaków ceny.
    
    Przykłady:
        "4 599,00 zł" -> "4599.00"
        "4599,00" -> "4599.00"
        "4 599" -> "4599.00"
    """
    if not raw_price or raw_price == "N/A":
        return "N/A"
    
    # Usuń wszystko oprócz cyfr, przecinka i kropki
    clean = re.sub(r'[^\d,.]', '', raw_price)
    
    # Usuń spacje
    clean = clean.replace(' ', '')
    
    # Zamień przecinek na kropkę (polski format)
    clean = clean.replace(',', '.')
    
    # Usuń wielokrotne kropki - zostaw tylko pierwszą
    if '.' in clean:
        parts = clean.split('.')
        # Pierwsza część to liczba całkowita, reszta to grosze
        integer_part = parts[0]
        decimal_parts = ''.join(parts[1:])
        
        # Jeśli są grosze, weź tylko pierwsze 2 cyfry
        if decimal_parts:
            clean = f"{integer_part}.{decimal_parts[:2]}"
        else:
            clean = f"{integer_part}.00"
    else:
        # Jeśli nie ma kropki, dodaj .00
        clean = f"{clean}.00"
    
    # Walidacja - sprawdź czy to rzeczywiście liczba
    try:
        float(clean)
        return clean
    except:
        return "N/A"


def extract_price_fast(page):
    """
    SZYBKA metoda wyciągania ceny - próbuje różne selektory
    bez czekania na lazy-load
    """
    # Lista selektorów od najbardziej do najmniej specyficznych
    selectors = [
        "[data-name='productPrice']",
        ".price-wrapper",
        "[class*='Price']",
        "span[class*='price']",
        "div[class*='price']",
    ]
    
    for selector in selectors:
        try:
            element = page.locator(selector).first
            if element.count() > 0:
                text = element.inner_text(timeout=2000).strip()
                if text and any(char.isdigit() for char in text):
                    return text
        except:
            continue
    
    return None


def extract_price_from_html(html):
    """
    FALLBACK: regex na całym HTML
    Szuka wzorca: cyfry + "zł"
    """
    matches = re.findall(r"(\d[\d\s]*[,.]?\d{0,2})\s*zł", html)
    if matches:
        # Zwróć pierwszą dopasowaną cenę
        return matches[0].replace("  ", " ").strip()
    return None

# =============================================================================
# GŁÓWNA LOGIKA SCRAPOWANIA
# =============================================================================

def scrape(page, url, is_first_request=False, custom_date=None):
      if is_first_request:
            page.screenshot(path="debug_screenshot.png", full_page=True)
            print(f"  🐛 DEBUG - tytuł strony: {page.title()}")
    """
    Scrapuje pojedynczy produkt
    
    Args:
        page: Obiekt strony Playwright
        url: URL do scrapowania
        is_first_request: Czy to pierwszy request (dłuższe czekanie na cookies)
        custom_date: Opcjonalna data (dla trybu backfill)
    """
    try:
        # Załaduj stronę
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        
        # Czekanie zależne od tego czy to pierwszy request
        if is_first_request:
            page.wait_for_timeout(2000)  # Dłużej na pierwsze ładowanie
            accept_cookies(page)
            page.wait_for_timeout(1000)  # Chwila po kliknięciu cookies
        else:
            page.wait_for_timeout(1000)  # Krócej - cookies już zaakceptowane
        
        # NAZWA - zawsze pierwsza
        try:
            name = page.locator("h1").first.inner_text(timeout=3000).strip()
        except:
            name = "N/A"
        
        # KATEGORIA - wykryj automatycznie
        category = detect_category(name, url)
        
        # CENA - FAST METHOD (bez długiego scrollowania)
        raw_price = extract_price_fast(page)
        
        # Jeśli fast method nie zadziałał, spróbuj regex na HTML
        if not raw_price:
            html = page.content()
            raw_price = extract_price_from_html(html)
        
        # Wyczyść cenę do samej liczby
        price = clean_price(raw_price) if raw_price else "N/A"
        
        # DOSTĘPNOŚĆ
        try:
            availability = page.locator("text=/produkt|dostęp|magazyn/i").first.inner_text(timeout=2000).strip()
        except:
            availability = "N/A"
        
        # Data - użyj custom_date jeśli podana, w przeciwnym razie dzisiejsza
        current_date = custom_date if custom_date else datetime.now().strftime("%Y-%m-%d")
        
        return {
            "date": current_date,
            "category": category,
            "name": name,
            "price": price,
            "availability": availability,
            "url": url
        }
    
    except Exception as e:
        current_date = custom_date if custom_date else datetime.now().strftime("%Y-%m-%d")
        return {
            "date": current_date,
            "category": "ERROR",
            "name": "ERROR",
            "price": "ERROR",
            "availability": str(e),
            "url": url
        }


def save(results):
    """Zapisuje wyniki do CSV (bez duplikatów)"""
    new_df = pd.DataFrame(results)
    
    if os.path.exists(OUTPUT_FILE):
        old_df = pd.read_csv(OUTPUT_FILE)
        
        # Usuń duplikaty (ta sama data + URL) - zostaw ostatni
        combined = pd.concat([old_df, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=['date', 'url'], keep='last')
        combined = combined.sort_values(['date', 'category', 'name'])
        combined.to_csv(OUTPUT_FILE, index=False)
    else:
        new_df.to_csv(OUTPUT_FILE, index=False)

# =============================================================================
# GŁÓWNA FUNKCJA
# =============================================================================

def main():
    """Główna funkcja programu"""
    urls = load_urls()
    
    # Tryb backfill lub normalny
    if BACKFILL_MODE:
        dates_to_process = BACKFILL_DATES
        print("=" * 60)
        print(f"🔄 TRYB BACKFILL: Uzupełniam {len(dates_to_process)} dni")
        print(f"📅 Daty: {', '.join(dates_to_process)}")
        print("=" * 60)
    else:
        dates_to_process = [None]  # None = dzisiejsza data
        print("=" * 60)
        print(f"🚀 Startuję scraping {len(urls)} produktów")
        print("=" * 60)
    
    print()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page(user_agent=USER_AGENT)
        
        for target_date in dates_to_process:
            if target_date:
                print(f"\n📆 Przetwarzam datę: {target_date}")
                print("-" * 60)
            
            results = []
            
            for i, url in enumerate(urls, 1):
                is_first = (i == 1)
                
                print(f"[{i}/{len(urls)}] 🔍 {url}")
                
                data = scrape(page, url, is_first_request=is_first, custom_date=target_date)
                results.append(data)
                
                print(f"  🏷️  {data['category']}")
                print(f"  📦 {data['name'][:50]}...")
                print(f"  💰 {data['price']} PLN")
                print("-" * 60)
                
                # Delay - dłuższy dla pierwszego, krótszy dla reszty
                if i < len(urls):
                    delay = DELAY_FIRST_REQUEST if is_first else DELAY_BETWEEN_REQUESTS
                    print(f"  ⏳ Czekam {delay}s...")
                    time.sleep(delay)
            
            save(results)
            print(f"\n✅ Zapisano dane dla {target_date or 'dzisiaj'}")
        
        browser.close()
    
    print("\n" + "=" * 60)
    print("✅ ZAKOŃCZONO")
    print("=" * 60)


if __name__ == "__main__":
    main()
