import os
import re
import time
import shutil

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException

# =========================================
# CONFIG
# =========================================

URL = "https://cfpp.nic.in/#/report/3/proc_procuring_agency/"
DOWNLOAD_DIR = os.path.abspath("procurement_data")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# =========================================
# DRIVER SETUP
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

# Uncomment below lines if running in GitHub Actions / Headless
# options.add_argument("--headless=new")
# options.add_argument("--no-sandbox")
# options.add_argument("--disable-dev-shm-usage")
# options.add_argument("--window-size=1920,1080")

driver = webdriver.Chrome(options=options)
driver.maximize_window()
driver.set_page_load_timeout(60)
driver.set_script_timeout(30)

# =========================================
# DYNAMIC WAITS & HELPERS
# =========================================

NO_DATA_PHRASES = [
    "no records found for the above selected combination",
    "no data", "no record", "not found", "no result",
    "data not available", "no information", "no state list found"
]

def sanitize(text: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", text.strip())

def get_existing_downloads() -> set:
    return {
        os.path.splitext(f)[0]
        for f in os.listdir(DOWNLOAD_DIR)
        if f.endswith((".xlsx", ".xls"))
    }

def wait_for_page_load(driver, timeout=30):
    """Dynamically waits for the Angular app to load the initial elements."""
    def page_ready(d):
        try:
            el = d.find_element(By.ID, "m_s_id")
            return len(Select(el).options) > 0
        except BaseException:
            return False
    WebDriverWait(driver, timeout).until(page_ready)

def get_select_options(driver, select_id) -> list[dict]:
    """Return list of valid {value, text} skipping the placeholder."""
    try:
        el = driver.find_element(By.ID, select_id)
        sel = Select(el)
        options = []
        for opt in sel.options:
            v = opt.get_attribute("value")
            t = opt.text.strip()
            if v and v != "0" and t:
                options.append({"value": v, "text": t})
        return options
    except BaseException:
        return []

def select_dropdown_dynamic(driver, parent_id, value, child_id=None):
    """
    Dynamically select an option and wait EXACTLY until the target child dropdown
    has its DOM rebuilt by Angular, eliminating the need for manual sleeps.
    """
    parent_el = WebDriverWait(driver, 15).until(
        EC.element_to_be_clickable((By.ID, parent_id))
    )
    
    old_child_id = None
    old_options = []
    
    if child_id:
        try:
            child_el = driver.find_element(By.ID, child_id)
            old_child_id = child_el.id
            old_options = [opt.text for opt in Select(child_el).options]
        except BaseException:
            pass

    Select(parent_el).select_by_value(value)

    if not child_id:
        return # No child dependency to wait for

    def child_updated(d):
        try:
            body = d.find_element(By.TAG_NAME, "body").text.lower()
            if any(phrase in body for phrase in NO_DATA_PHRASES):
                return True # Triggered a no-data state
            
            new_child = d.find_element(By.ID, child_id)
            if new_child.id != old_child_id:
                return True # DOM Element was rebuilt
            
            new_options = [opt.text for opt in Select(new_child).options]
            if new_options != old_options:
                return True # Data changed
                
            return False
        except StaleElementReferenceException:
            return True # Currently rebuilding
        except BaseException:
            return False

    try:
        # A short timeout because occasionally the site returns identical unchanged data
        WebDriverWait(driver, 5).until(child_updated)
    except TimeoutException:
        pass

def click_submit_and_wait(driver, wait_timeout=60):
    """
    Dynamically click submit and monitor the exact Table Row DOM IDs to 
    ensure we don't accidentally grab stale data from a previous search.
    """
    try:
        old_rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        old_row_ids = {row.id for row in old_rows}
    except BaseException:
        old_row_ids = set()

    submit_btn = WebDriverWait(driver, 15).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']"))
    )
    driver.execute_script("arguments[0].click();", submit_btn)

    def table_updated(d):
        try:
            body = d.find_element(By.TAG_NAME, "body").text.lower()
            if any(phrase in body for phrase in NO_DATA_PHRASES):
                return "no_data"
            
            current_rows = d.find_elements(By.CSS_SELECTOR, "table tbody tr")
            if not current_rows:
                return False # Still loading rows
                
            # Guarantee these are new rows, not ghost rows from the previous run
            if old_row_ids and current_rows[0].id in old_row_ids:
                return False 
                
            row_text = "".join(r.text.strip() for r in current_rows)
            if len(row_text) > 10:
                return "ok"
                
            return False
        except StaleElementReferenceException:
            return False
        except BaseException:
            return False

    try:
        return WebDriverWait(driver, wait_timeout).until(table_updated)
    except TimeoutException:
        return "timeout"

def click_excel_button(driver):
    btn = WebDriverWait(driver, 15).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "button[aria-label='Download Report in Excel Format']"))
    )
    driver.execute_script("arguments[0].click();", btn)

def wait_for_download(directory: str, before: set, timeout: int = 60) -> str | None:
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
        time.sleep(0.5) # OS File watcher polling (not a UI wait)
    return None


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
total_saved, total_skipped, total_no_data, total_errors = 0, 0, 0, 0

print(f"\n{'='*60}")
print(f"  Procurement data downloader (Dynamic Waits)")
print(f"  Saving to: {DOWNLOAD_DIR}")
print(f"  Already downloaded: {len(already_done)} file(s) — will skip these")
print(f"{'='*60}")

try:
    for attempt in range(3):
        try:
            driver.get(URL)
            wait_for_page_load(driver)
            break
        except Exception as load_err:
            print(f"  Page load attempt {attempt+1} failed: {load_err}")
            if attempt == 2: raise
            time.sleep(5) # Network error backoff

    for season in MARKETING_SEASONS:
        for year in MARKETING_YEARS:
            season_text, year_text = season["text"], year["text"]
            print(f"\n► {season_text}  {year_text}")

            try:
                # Select Parent Dropdowns dynamically
                select_dropdown_dynamic(driver, "m_s_id", season["value"], child_id="comdty_id")
                select_dropdown_dynamic(driver, "m_year", year["value"], child_id="comdty_id")

                commodities = get_select_options(driver, "comdty_id")
                print(f"  {len(commodities)} commodities")
                if not commodities: continue

                for commodity in commodities:
                    commodity_text = commodity["text"]
                    print(f"  ┌─ {commodity_text}")

                    try:
                        select_dropdown_dynamic(driver, "comdty_id", commodity["value"], child_id="c_type_id")
                        crop_types = get_select_options(driver, "c_type_id")
                        if not crop_types: continue

                        for crop in crop_types:
                            crop_text = crop["text"]
                            print(f"  │  ├─ {crop_text}")

                            try:
                                select_dropdown_dynamic(driver, "c_type_id", crop["value"], child_id="st_id")
                                states = get_select_options(driver, "st_id")
                                if not states: continue
                                
                                for state in states:
                                    state_text = state["text"]
                                    print(f"  │  │  ├─ {state_text} ... ", end="", flush=True)

                                    fname = sanitize(f"{season_text}_{year_text}_{commodity_text}_{crop_text}_{state_text}")
                                    if fname in already_done:
                                        print(f"skipped")
                                        total_skipped += 1
                                        continue

                                    try:
                                        # State is the last selection, so no child_id to wait for
                                        select_dropdown_dynamic(driver, "st_id", state["value"])

                                        # Submit and dynamically wait for the new DOM to render
                                        table_status = click_submit_and_wait(driver)
                                        
                                        if table_status == "no_data":
                                            print("no data")
                                            total_no_data += 1
                                            continue
                                        elif table_status == "timeout":
                                            print("timed out waiting for table")
                                            total_errors += 1
                                            continue

                                        before = set(os.listdir(DOWNLOAD_DIR))
                                        click_excel_button(driver)

                                        downloaded = wait_for_download(DOWNLOAD_DIR, before)
                                        if not downloaded:
                                            print("download timed out")
                                            total_errors += 1
                                            continue

                                        ext = os.path.splitext(downloaded)[1]
                                        target_path = os.path.join(DOWNLOAD_DIR, fname + ext)

                                        if os.path.exists(target_path): os.remove(target_path)
                                        os.rename(downloaded, target_path)
                                        already_done.add(fname)
                                        
                                        print("saved")
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
                driver.get(URL)
                wait_for_page_load(driver)
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