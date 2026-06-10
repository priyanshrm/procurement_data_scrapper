import os
import re
import time
import json

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
)

# =========================================
# CONFIG
# =========================================

URL = "https://cfpp.nic.in/#/report/3/proc_procuring_agency/"
DOWNLOAD_DIR = os.path.abspath("procurement_data")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FAILED_FILE = "failed_combinations.json"
MAX_RETRIES = 3
MIN_FILE_SIZE = 5 * 1024  # 5 KB minimum

EXCLUDE_2026_27 = True

# =========================================
# DRIVER
# =========================================

options = webdriver.ChromeOptions()
prefs = {
    "download.default_directory": DOWNLOAD_DIR,
    "download.prompt_for_download": False,
    "download.directory_upgrade": True,
    "safebrowsing.enabled": True,
}
options.add_experimental_option("prefs", prefs)

options.add_argument("--headless=new")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")

driver = webdriver.Chrome(options=options)

# =========================================
# HELPERS
# =========================================

def sanitize(text):
    return re.sub(r'[\\/*?:"<>|]', "_", text.strip())


def retry_action(func, *args, retries=MAX_RETRIES, delay=1, **kwargs):
    for i in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if i == retries - 1:
                raise e
            time.sleep(delay)


def purge_dom():
    driver.execute_script("""
        document.querySelectorAll('.missing_field, .alert-danger-msg').forEach(el => el.remove());
        const tbody = document.querySelector('table tbody');
        if (tbody) tbody.innerHTML = '';
    """)


# =========================================
# STRICT DROPDOWN
# =========================================

def wait_for_dropdown_strict(select_id, timeout=15):
    end = time.time() + timeout
    last = None
    stable_since = None

    while time.time() < end:
        try:
            el = driver.find_element(By.ID, select_id)

            if el.get_attribute("disabled"):
                time.sleep(0.3)
                continue

            opts = []
            for o in Select(el).options:
                val = o.get_attribute("value")
                txt = o.text.strip()
                if val and val != "0":
                    opts.append((val, txt))

            if not opts:
                last = None
                stable_since = None
                time.sleep(0.3)
                continue

            if opts == last:
                if stable_since and (time.time() - stable_since > 1.2):
                    return [{"value": v, "text": t} for v, t in opts]
            else:
                last = opts
                stable_since = time.time()

        except (StaleElementReferenceException, NoSuchElementException):
            last = None
            stable_since = None

        time.sleep(0.3)

    return []


# =========================================
# ACTIONS
# =========================================

def select_value(select_id, value):
    purge_dom()
    el = WebDriverWait(driver, 15).until(
        EC.element_to_be_clickable((By.ID, select_id))
    )
    Select(el).select_by_value(value)


def submit_and_wait():
    btn = WebDriverWait(driver, 15).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']"))
    )
    driver.execute_script("arguments[0].click();", btn)

    end = time.time() + 30
    while time.time() < end:
        alerts = driver.find_elements(By.CSS_SELECTOR, ".alert-danger-msg")
        if alerts and "no records" in alerts[0].text.lower():
            return "no_data"

        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if rows and len(rows) > 1:
            return "ok"

        time.sleep(0.3)

    return "timeout"


def wait_download(before):
    end = time.time() + 60
    while time.time() < end:
        files = set(os.listdir(DOWNLOAD_DIR))
        new = files - before

        for f in new:
            if f.endswith((".xlsx", ".xls")):
                path = os.path.join(DOWNLOAD_DIR, f)
                if os.path.exists(path) and os.path.getsize(path) > MIN_FILE_SIZE:
                    if not any(x.endswith(".crdownload") for x in files):
                        return path

        time.sleep(0.5)

    return None


# =========================================
# FAILURE TRACKING
# =========================================

def load_failed():
    if os.path.exists(FAILED_FILE):
        with open(FAILED_FILE) as f:
            return json.load(f)
    return []


def save_failed(data):
    with open(FAILED_FILE, "w") as f:
        json.dump(data, f, indent=2)


# =========================================
# CORE PROCESSOR
# =========================================

def process_combination(combo):
    season, year, comm, crop, state = combo

    fname = sanitize(f"{season['text']}_{year['text']}_{comm['text']}_{crop['text']}_{state['text']}")
    path = os.path.join(DOWNLOAD_DIR, fname + ".xlsx")

    if os.path.exists(path):
        return "skip"

    select_value("m_s_id", season["value"])
    select_value("m_year", year["value"])
    select_value("comdty_id", comm["value"])
    select_value("c_type_id", crop["value"])
    select_value("st_id", state["value"])

    status = submit_and_wait()

    if status != "ok":
        return status

    before = set(os.listdir(DOWNLOAD_DIR))

    btn = WebDriverWait(driver, 15).until(
        EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button[aria-label='Download Report in Excel Format']")
        )
    )
    driver.execute_script("arguments[0].click();", btn)

    file = wait_download(before)
    if not file:
        return "download_fail"

    os.rename(file, path)
    return "saved"


# =========================================
# MAIN
# =========================================

MARKETING_SEASONS = [{"value": "1", "text": "KMS"}, {"value": "2", "text": "RMS"}]
MARKETING_YEARS = [
    {"value": "2025-2026", "text": "2025-2026"},
    {"value": "2024-2025", "text": "2024-2025"},
    {"value": "2023-2024", "text": "2023-2024"},
]

failed = load_failed()
new_failed = []

driver.get(URL)

for season in MARKETING_SEASONS:
    for year in MARKETING_YEARS:

        select_value("m_s_id", season["value"])
        select_value("m_year", year["value"])

        commodities = wait_for_dropdown_strict("comdty_id")
        if not commodities:
            continue

        for comm in commodities:

            select_value("comdty_id", comm["value"])
            crops = wait_for_dropdown_strict("c_type_id")

            for crop in crops:

                select_value("c_type_id", crop["value"])
                states = wait_for_dropdown_strict("st_id")

                for state in states:

                    combo = (season, year, comm, crop, state)

                    try:
                        result = retry_action(process_combination, combo)

                        print(f"{comm['text']} | {crop['text']} | {state['text']} → {result}")

                        if result not in ["saved", "skip", "no_data"]:
                            new_failed.append(combo)

                    except Exception:
                        new_failed.append(combo)


save_failed(new_failed)
driver.quit()