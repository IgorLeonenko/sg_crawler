"""Crawler for locating Solar Guitars listings that show a zero price."""

from __future__ import annotations

import json
import time
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import List, Set, Tuple

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency
    load_dotenv = None

LISTING_URL_TEMPLATE = "https://www.solar-guitars.com/shop/page/{page}/"
PAGE_RANGE = range(1, 11)
STATIC_LISTING_URLS = [
    "https://www.solar-guitars.com/categorie-produit/pedals/",
    "https://www.solar-guitars.com/outlet-store/",
    "https://www.solar-guitars.com/categorie-produit/accessories/",
]
RESULTS_PATH = Path("results.json")

@dataclass
class Listing:
    title: str
    price_text: str
    link: str

    def to_dict(self) -> dict:
        return {"title": self.title, "price": self.price_text, "link": self.link}


def load_existing_results() -> Tuple[List[dict], Set[str]]:
    """Return stored entries and a set of known links."""
    if not RESULTS_PATH.exists():
        return [], set()
    try:
        data = json.loads(RESULTS_PATH.read_text())
        if not isinstance(data, list):
            raise ValueError("Results file must contain a list.")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Failed to read {RESULTS_PATH}: {exc}. Rebuilding file.")
        return [], set()
    deduped: List[dict] = []
    links: Set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        link = entry.get("link")
        if not isinstance(link, str) or not link or link in links:
            continue
        links.add(link)
        deduped.append(entry)
    return deduped, links


def save_results(entries: List[dict]) -> None:
    """Persist entries to disk."""
    RESULTS_PATH.write_text(json.dumps(entries, indent=2))


def load_env_file() -> None:
    """Load .env if python-dotenv is present."""
    env_path = Path(".env")
    if not env_path.exists():
        return
    if load_dotenv is None:
        print("python-dotenv not installed; .env file ignored.")
        return
    load_dotenv(dotenv_path=env_path)


def _bool_from_env(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def get_email_settings() -> Tuple[str, int, str, str, str, str, bool] | None:
    """Collect SMTP configuration from env vars."""
    host = os.getenv("EMAIL_HOST", "smtp.gmail.com")
    port = int(os.getenv("EMAIL_PORT", "587"))
    user = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASSWORD")
    sender = os.getenv("EMAIL_FROM", user)
    recipient = os.getenv("EMAIL_TO")
    use_tls = _bool_from_env("EMAIL_USE_TLS", True)

    if not user or not password:
        print("EMAIL_USER or EMAIL_PASSWORD missing; skipping email notification.")
        return None
    if not sender:
        sender = user
    if not recipient:
        print("EMAIL_TO missing and default recipient empty; skipping email notification.")
        return None
    return host, port, user, password, sender, recipient, use_tls


def send_email_notification(listings: List[dict]) -> None:
    """Send a summary email for any new listings."""
    settings = get_email_settings()
    if not settings or not listings:
        return
    host, port, user, password, sender, recipient, use_tls = settings
    lines = [
        f"Found {len(listings)} new zero-price listing(s):",
        "",
    ]
    for entry in listings:
        lines.append(
            f"- {entry.get('title', 'Unknown')} ({entry.get('price', '€0.00')}): {entry.get('link')}"
        )
    body = "\n".join(lines)
    msg = EmailMessage()
    msg["Subject"] = f"[Solar crawler] {len(listings)} new zero-price listing(s) found"
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            if use_tls:
                server.starttls()
            server.login(user, password)
            server.send_message(msg)
        print(f"Notification email sent to {recipient}.")
    except Exception as exc:
        print(f"Failed to send notification email: {exc}")


def build_driver(headless: bool = True) -> webdriver.Chrome:
    """Configure a Chrome driver instance."""
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1440,900")
    chrome_options.add_argument("--no-sandbox")
    try:
        from webdriver_manager.chrome import ChromeDriverManager
    except ImportError as exc:
        raise RuntimeError(
            "webdriver-manager is required; install it via 'pip install webdriver-manager'."
        ) from exc
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(options=chrome_options, service=service)
    driver.set_page_load_timeout(60)
    return driver


def wait_for_listings(driver: webdriver.Chrome, timeout: int = 30) -> None:
    """Block until the listings container appears."""
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "ul.listing-products"))
    )


def scroll_page(driver: webdriver.Chrome, pause: float = 0.75) -> None:
    """Scroll down gradually so lazy-loaded tiles appear."""
    prev_height = driver.execute_script("return document.body.scrollHeight")
    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(pause)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == prev_height:
            break
        prev_height = new_height


def _read_price_value(element: WebElement) -> tuple[str | None, float | None]:
    """Extract text representation and numeric value of the price, if any."""
    price_text: str | None = None
    price_value: float | None = None
    try:
        hidden = element.find_element(By.CSS_SELECTOR, ".price_for_filter")
        hidden_text = hidden.get_attribute("textContent").strip()
        if hidden_text:
            price_value = float(hidden_text.replace(",", ""))
    except NoSuchElementException:
        pass

    try:
        visible = element.find_element(By.CSS_SELECTOR, ".wcpbc-price")
        price_text = visible.text.strip()
        if price_value is None and price_text:
            numeric = (
                price_text.replace("€", "")
                .replace("$", "")
                .replace(",", "")
                .split()  # there might be both original and sale prices
            )
            if numeric:
                price_value = float(numeric[-1])
    except NoSuchElementException:
        pass

    return price_text, price_value


def extract_listing_info(element: WebElement) -> Listing | None:
    """Return listing details if the price is zero, else None."""
    price_text, price_value = _read_price_value(element)
    if price_value is None or price_value != 0.0:
        return None

    link_element = element.find_element(By.CSS_SELECTOR, "a.totallink")
    title_element = element.find_element(By.CSS_SELECTOR, ".item-compare-title")
    return Listing(
        title=title_element.text.strip(),
        price_text=price_text or "€0.00",
        link=link_element.get_attribute("href"),
    )


def collect_free_listings_on_url(driver: webdriver.Chrome, url: str) -> List[Listing]:
    """Grab zero-priced listings from a specific listing url."""
    try:
        driver.get(url)
    except TimeoutException:
        print(f"Timed out loading {url}, skipping.")
        driver.execute_script("window.stop();")
        return []
    except WebDriverException as exc:
        print(f"Failed to load {url}: {exc.msg}")
        return []
    wait_for_listings(driver)
    scroll_page(driver)
    items = driver.find_elements(By.CSS_SELECTOR, "ul.listing-products li.listing-product")
    free_entries: List[Listing] = []
    for item in items:
        listing = extract_listing_info(item)
        if listing:
            free_entries.append(listing)
    return free_entries


def collect_free_listings_on_page(driver: webdriver.Chrome, page_number: int) -> List[Listing]:
    """Wrapper to handle paginated /shop URLs."""
    return collect_free_listings_on_url(
        driver, LISTING_URL_TEMPLATE.format(page=page_number)
    )


def main() -> None:
    load_env_file()
    driver = build_driver(headless=True)
    try:
        run_listings: List[Listing] = []
        for page in PAGE_RANGE:
            page_listings = collect_free_listings_on_page(driver, page)
            run_listings.extend(page_listings)
            print(f"Page {page} scanned, zero-price items found: {len(page_listings)}")
        for url in STATIC_LISTING_URLS:
            url_listings = collect_free_listings_on_url(driver, url)
            run_listings.extend(url_listings)
            print(f"{url} scanned, zero-price items found: {len(url_listings)}")

        stored_entries, known_links = load_existing_results()
        new_entries: List[dict] = []
        for listing in run_listings:
            if listing.link in known_links:
                continue
            known_links.add(listing.link)
            entry = listing.to_dict()
            stored_entries.append(entry)
            new_entries.append(entry)

        save_results(stored_entries)
        print(json.dumps(new_entries, indent=2))
        if new_entries:
            send_email_notification(new_entries)
        else:
            print("No new zero-price listings discovered this run.")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
