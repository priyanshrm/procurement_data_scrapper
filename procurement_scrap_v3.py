import os
import re
import time
import shutil

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# =========================================
# CONFIG
# =========================================

URL = "https://cfpp.nic.in/#/report/3/proc_procuring_agency/"
DOWNLOAD_DIR = os.path.abspath("procurement_data")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# =========================================
# DRIVER SETUP — direct downloads to DOWNLOAD_DIR
# =========================================

# =========================================
# DRIVER SETUP — direct downloads to DOWNLOAD_DIR
# =========================================

options = webdriver.ChromeOptions()
prefs = {
    "download.default_directory": DOWNLOAD_DIR,
    "download.prompt_for_download": False,
    "download.directory_upgrade": True,
    "safebrowsing.enabled": True,
    "profile.default_content_setting_values.automatic_downloads": 1,
    "profile.content_settings.exceptions.automatic_downloads.*.setting": 1,
}
options.add_experimental_option("prefs", prefs)

# --- NEW: GitHub Actions specific arguments ---
options.add_argument("--headless=new") # Run without UI
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--window-size=1920,1080") # Ensure elements are in view
# ----------------------------------------------

driver = webdriver.Chrome(options=options)
driver.maximize_window()
driver.set_page_load_timeout(60)        
driver.set_script_timeout(30)           
wait = WebDriverWait(driver, 30)
# =========================================
# HELPERS
# =========================================

def sanitize(text: str) -> str:
    """Make a string safe for use in a filename."""
    return re.sub(r'[\\/*?:"<>|]', "_", text.strip())

def wait_for_download(directory: str, before: set, timeout: int = 60) -> str | None:
    """
    Wait until a NEW .xlsx/.xls file appears in directory that was not
    present in `before` (snapshot taken before clicking Excel), and is
    fully written (no matching .crdownload temp file present).
    Returns the full path of the new file, or None on timeout.
    """
    end_time = time.time() + timeout
    while time.time() < end_time:
        current = set(os.listdir(directory))
        new_files = [
            f for f in (current - before)
            if f.endswith((".xlsx", ".xls")) and not f.endswith(".crdownload")
        ]
        crdownloads = [f for f in current if f.endswith(".crdownload")]
        if new_files and not crdownloads:
            return os.path.join(directory, new_files[0])
        time.sleep(1)
    return None

def get_existing_downloads() -> set:
    """Return set of filenames already in DOWNLOAD_DIR (without extension)."""
    return {
        os.path.splitext(f)[0]
        for f in os.listdir(DOWNLOAD_DIR)
        if f.endswith((".xlsx", ".xls"))
    }

def select_option_by_value(select_el, value: str):
    """Select a <select> option by its value attribute."""
    sel = Select(select_el)
    sel.select_by_value(value)

def get_select_options(select_el) -> list[dict]:
    """
    Return list of {value, text} for all non-placeholder options
    (skips options with value '0' or empty value).
    """
    sel = Select(select_el)
    options = []
    for opt in sel.options:
        v = opt.get_attribute("value")
        t = opt.text.strip()
        if v and v != "0" and t:
            options.append({"value": v, "text": t})
    return options

# Phrases the site may show when there is no data
NO_DATA_PHRASES = [
    "no records found for the above selected combination",
    "no data", "no record", "not found", "no result",
    "data not available", "no information", "no state list found"
]

def get_page_snapshot() -> str:
    """Return current body text — used to detect when the page has refreshed."""
    try:
        return driver.find_element(By.TAG_NAME, "body").text
    except:
        return ""

def wait_for_table(old_snapshot: str, timeout: int = 20) -> str:
    """
    Wait for the page to respond after Submit. Returns:
      "ok"      — table has real data rows
      "no_data" — site shows no-data message
      "timeout" — nothing appeared within timeout
    """
    end_time = time.time() + timeout

    # Step 1: wait for page to change from the previous state
    while time.time() < end_time:
        current = get_page_snapshot()
        if current != old_snapshot:
            break
        time.sleep(0.5)
    else:
        return "timeout"

    # Step 2: now read the new state
    time.sleep(0.5)  # let Angular finish rendering
    body_text = get_page_snapshot().lower()

    if any(phrase in body_text for phrase in NO_DATA_PHRASES):
        return "no_data"

    rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
    if rows:
        row_text = " ".join(r.text.strip().lower() for r in rows)
        if row_text.strip():
            return "ok"

    return "timeout"

def click_excel_button():
    """Click the Excel download button."""
    btn = wait.until(
        EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button[aria-label='Download Report in Excel Format']")
        )
    )
    driver.execute_script("arguments[0].click();", btn)

# =========================================
# FIXED DROPDOWN VALUES
# =========================================

MARKETING_SEASONS = [
    {"value": "1", "text": "KMS"},
    {"value": "2", "text": "RMS"},
]

MARKETING_YEARS = [
    {"value": "2025-2026", "text": "2025-2026"},
    {"value": "2024-2025", "text": "2024-2025"},
    {"value": "2023-2024", "text": "2023-2024"},
    {"value": "2022-2023", "text": "2022-2023"},
    {"value": "2021-2022", "text": "2021-2022"},
]

# =========================================
# MAIN LOOP
# =========================================

already_done = get_existing_downloads()
total_saved = 0
total_skipped = 0
total_no_data = 0
total_errors = 0
print(f"\n{'='*60}")
print(f"  Procurement data downloader")
print(f"  Saving to: {DOWNLOAD_DIR}")
print(f"  Already downloaded: {len(already_done)} file(s) — will skip these")
print(f"{'='*60}")

try:
    for attempt in range(3):
        try:
            driver.get(URL)
            time.sleep(3)  # let Angular render
            break
        except Exception as load_err:
            print(f"  Page load attempt {attempt+1} failed: {load_err}")
            if attempt == 2:
                raise
            time.sleep(5)

    for season in MARKETING_SEASONS:
        for year in MARKETING_YEARS:

            season_text = season["text"]
            year_text   = year["text"]

            print(f"\n► {season_text}  {year_text}")

            try:
                # ----------------------------------
                # SELECT MARKETING SEASON
                # ----------------------------------
                season_el = wait.until(EC.presence_of_element_located((By.ID, "m_s_id")))
                select_option_by_value(season_el, season["value"])
                time.sleep(1)

                # ----------------------------------
                # SELECT MARKETING YEAR
                # ----------------------------------
                year_el = wait.until(EC.presence_of_element_located((By.ID, "m_year")))
                select_option_by_value(year_el, year["value"])
                time.sleep(1)

                # ----------------------------------
                # READ COMMODITY OPTIONS (dynamic)
                # ----------------------------------
                commodity_el = wait.until(EC.presence_of_element_located((By.ID, "comdty_id")))
                commodities = get_select_options(commodity_el)
                print(f"  {len(commodities)} commodit{'y' if len(commodities)==1 else 'ies'}: {', '.join(c['text'] for c in commodities)}")

                if not commodities:
                    print("  └─ no commodities available, skipping")
                    continue

                for commodity in commodities:
                    commodity_text = commodity["text"]
                    print(f"  ┌─ {commodity_text}")

                    try:
                        # Select commodity
                        commodity_el = wait.until(EC.presence_of_element_located((By.ID, "comdty_id")))
                        select_option_by_value(commodity_el, commodity["value"])
                        time.sleep(1)

                        # ----------------------------------
                        # READ CROP TYPE OPTIONS (dynamic)
                        # ----------------------------------
                        crop_el = wait.until(EC.presence_of_element_located((By.ID, "c_type_id")))
                        crop_types = get_select_options(crop_el)
                        print(f"  │  crop types: {', '.join(c['text'] for c in crop_types)}")

                        if not crop_types:
                            print("  │  └─ no crop types available, skipping")
                            continue

                        for crop in crop_types:
                            crop_text = crop["text"]
                            print(f"  │  ├─ {crop_text}")

                            try:
                                # Select crop type
                                crop_el = wait.until(EC.presence_of_element_located((By.ID, "c_type_id")))
                                select_option_by_value(crop_el, crop["value"])
                                time.sleep(1)

                                # ----------------------------------
                                # READ STATE OPTIONS (dynamic)
                                # ----------------------------------
                                state_el = wait.until(EC.presence_of_element_located((By.ID, "st_id")))
                                states = get_select_options(state_el)
                                
                                if not states:
                                    print("  │  │  └─ no states available, skipping")
                                    continue
                                
                                for state in states:
                                    state_text = state["text"]
                                    print(f"  │  │  ├─ {state_text} ... ", end="", flush=True)

                                    # Build target filename (without extension)
                                    fname = sanitize(f"{season_text}_{year_text}_{commodity_text}_{crop_text}_{state_text}")

                                    # Skip if already downloaded
                                    if fname in already_done:
                                        print(f"already downloaded, skipping")
                                        total_skipped += 1
                                        continue

                                    try:
                                        # Select state
                                        state_el = wait.until(EC.presence_of_element_located((By.ID, "st_id")))
                                        select_option_by_value(state_el, state["value"])
                                        time.sleep(1)

                                        # Snapshot page before Submit so we can detect change
                                        pre_submit_snapshot = get_page_snapshot()

                                        # Click Submit
                                        submit_btn = wait.until(
                                            EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']"))
                                        )
                                        driver.execute_script("arguments[0].click();", submit_btn)

                                        # Wait for table data to load
                                        table_status = wait_for_table(pre_submit_snapshot)
                                        if table_status == "no_data":
                                            print("no data")
                                            total_no_data += 1
                                            continue
                                        elif table_status == "timeout":
                                            print("timed out waiting for table")
                                            total_errors += 1
                                            continue

                                        # Snapshot directory before triggering download
                                        before = set(os.listdir(DOWNLOAD_DIR))

                                        # Click Excel button
                                        click_excel_button()

                                        # Wait specifically for a NEW file to appear
                                        downloaded = wait_for_download(DOWNLOAD_DIR, before)
                                        if not downloaded:
                                            print("download timed out")
                                            total_errors += 1
                                            continue

                                        # Rename to meaningful name including the State
                                        ext = os.path.splitext(downloaded)[1]
                                        target_path = os.path.join(DOWNLOAD_DIR, fname + ext)

                                        # If a file with the same name already exists, remove it
                                        if os.path.exists(target_path):
                                            os.remove(target_path)

                                        os.rename(downloaded, target_path)
                                        already_done.add(fname)
                                        print(f"saved")
                                        total_saved += 1

                                    except Exception as state_err:
                                        print(f"error: {state_err}")
                                        total_errors += 1
                                        continue

                            except Exception as crop_err:
                                print(f"  │  └─ ERROR in {crop_text}: {crop_err}")
                                total_errors += 1
                                continue

                    except Exception as commodity_err:
                        print(f"  └─ ERROR in {commodity_text}: {commodity_err}")
                        total_errors += 1
                        continue

            except Exception as combo_err:
                import traceback
                print(f"\n  ERROR in {season_text} {year_text}:")
                traceback.print_exc()
                # Reload page and continue
                driver.get(URL)
                time.sleep(3)
                continue

except Exception as e:
    import traceback
    print("\n[MAIN ERROR]")
    traceback.print_exc()

finally:
    print(f"\n{'='*60}")
    print(f"  Run complete")
    print(f"  Saved    : {total_saved}")
    print(f"  Skipped  : {total_skipped}  (already existed)")
    print(f"  No data  : {total_no_data}")
    print(f"  Errors   : {total_errors}")
    print(f"  Location : {DOWNLOAD_DIR}")
    print(f"{'='*60}")
    driver.quit()