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

options.add_argument("--headless=new")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--window-size=1920,1080")

driver = webdriver.Chrome(options=options)
driver.maximize_window()
driver.set_page_load_timeout(60)
driver.set_script_timeout(30)

# =========================================
# HIGH-INTEGRITY DYNAMIC WAITS & HELPERS
# =========================================

def sanitize(text: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", text.strip())

def get_existing_downloads() -> set:
    return {
        os.path.splitext(f)[0]
        for f in os.listdir(DOWNLOAD_DIR)
        if f.endswith((".xlsx", ".xls"))
    }

def wait_for_page_load(driver, timeout=30):
    def page_ready(d):
        try:
            el = d.find_element(By.ID, "m_s_id")
            return len(Select(el).options) > 0
        except BaseException:
            return False
    WebDriverWait(driver, timeout).until(page_ready)

def check_for_missing_field_warning(driver) -> bool:
    """
    Scans the DOM immediately for UI configuration alerts indicating 
    that no child elements exist for the current selection.
    """
    try:
        warnings = driver.find_elements(By.CSS_SELECTOR, ".missing_field")
        for w in warnings:
            if w.is_displayed() and "no state list found" in w.text.lower():
                return True
    except BaseException:
        pass
    return False

def get_select_options(driver, select_id) -> list[dict]:
    try:
        el = driver.find_element(By.ID, select_id)
        
        # Fast exit if Angular explicitly disabled the element
        if el.get_attribute("disabled") is not None:
            return []
            
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

def select_dropdown_and_stabilize(driver, parent_id, value, child_id=None, timeout=12):
    """
    Selects a dropdown option, then samples the child element until its structure 
    completely settles for 400ms. Aborts early if a missing_field error appears.
    """
    parent_el = WebDriverWait(driver, 15).until(
        EC.element_to_be_clickable((By.ID, parent_id))
    )
    Select(parent_el).select_by_value(value)
    
    if not child_id:
        time.sleep(0.2)
        return

    end_time = time.time() + timeout
    last_state = None
    stable_start = None
    
    while time.time() < end_time:
        if check_for_missing_field_warning(driver):
            return  # Angular rendered an explicit empty warning block; exit wait cascade early.

        try:
            child_el = driver.find_element(By.ID, child_id)
            
            # If the target dropdown was intentionally disabled by the application UI
            if child_el.get_attribute("disabled") is not None:
                return
                
            options = [opt.get_attribute("value") for opt in Select(child_el).options]
            current_state = (child_el.id, tuple(options))
            
            if current_state == last_state:
                if stable_start is None:
                    stable_start = time.time()
                elif time.time() - stable_start >= 0.4:
                    return
            else:
                last_state = current_state
                stable_start = None
        except (StaleElementReferenceException, NoSuchElementException):
            last_state = None
            stable_start = None
        time.sleep(0.1)

def click_submit_and_wait(driver, wait_timeout=45):
    """
    Tracks state transitions reliably without modifying DOM nodes directly.
    """
    old_alert_el = None
    try:
        old_alert_el = driver.find_element(By.CSS_SELECTOR, ".alert-danger-msg")
    except NoSuchElementException:
        pass

    old_first_row_el = None
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if rows:
            old_first_row_el = rows[0]
    except BaseException:
        pass

    submit_btn = WebDriverWait(driver, 15).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']"))
    )
    driver.execute_script("arguments[0].click();", submit_btn)

    time.sleep(1.0)

    end_time = time.time() + wait_timeout
    while time.time() < end_time:
        try:
            alerts = driver.find_elements(By.CSS_SELECTOR, ".alert-danger-msg")
            if alerts:
                current_alert = alerts[0]
                if "no records found" in current_alert.text.lower():
                    if old_alert_el is None or current_alert != old_alert_el:
                        return "no_data"
                    elif old_first_row_el:
                        try:
                            _ = old_first_row_el.text
                        except StaleElementReferenceException:
                            return "no_data"
        except BaseException:
            pass

        try:
            rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
            if rows:
                current_first_row = rows[0]
                if old_first_row_el is None or current_first_row != old_first_row_el:
                    if len(current_first_row.text.strip()) > 2:
                        return "ok"
                else:
                    try:
                        _ = old_first_row_el.text
                    except StaleElementReferenceException:
                        return "ok"
        except BaseException:
            pass

        time.sleep(0.2)

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
        time.sleep(0.5)
    return None

# =========================================
# DATA SEED STRUCTS
# =========================================

MARKETING_SEASONS = [
    {"value": "1", "text": "KMS"},
    {"value": "2", "text": "RMS"},
]

MARKETING_YEARS = [
    {"value": "2026-2027", "text": "2026-2027"},  # Updated to reflect new selection options
    {"value": "2025-2026", "text": "2025-2026"},
    {"value": "2024-2025", "text": "2024-2025"},
    {"value": "2023-2024", "text": "2023-2024"},
    {"value": "2022-2023", "text": "2022-2023"},
    {"value": "2021-2022", "text": "2021-2022"},
]

# =========================================
# RUN ENGINE LOOP
# =========================================

already_done = get_existing_downloads()
total_saved, total_skipped, total_no_data, total_errors = 0, 0, 0, 0

print(f"\n{'='*60}")
print(f"  Procurement data downloader (Max Integrity Configuration Build)")
print(f"  Saving to: {DOWNLOAD_DIR}")
print(f"  Already downloaded: {len(already_done)} file(s)")
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
            time.sleep(5)

    for season in MARKETING_SEASONS:
        for year in MARKETING_YEARS:
            season_text, year_text = season["text"], year["text"]
            print(f"\n► {season_text}  {year_text}")

            try:
                select_dropdown_and_stabilize(driver, "m_s_id", season["value"], child_id="m_year")
                select_dropdown_and_stabilize(driver, "m_year", year["value"], child_id="comdty_id")

                if check_for_missing_field_warning(driver):
                    print("  (No commodities available for this configuration)")
                    continue

                commodities = get_select_options(driver, "comdty_id")
                print(f"  {len(commodities)} commodities identified")
                if not commodities: continue

                for commodity in commodities:
                    commodity_text = commodity["text"]
                    print(f"  ┌─ {commodity_text}")

                    try:
                        select_dropdown_and_stabilize(driver, "comdty_id", commodity["value"], child_id="c_type_id")
                        
                        if check_for_missing_field_warning(driver):
                            print("  │  └─ (No crop types available)")
                            continue

                        crop_types = get_select_options(driver, "c_type_id")
                        if not crop_types: continue

                        for crop in crop_types:
                            crop_text = crop["text"]
                            print(f"  │  ├─ {crop_text}")

                            try:
                                select_dropdown_and_stabilize(driver, "c_type_id", crop["value"], child_id="st_id")
                                
                                # Safety Intercept: Check if the selection combination has generated an empty state layout
                                if check_for_missing_field_warning(driver):
                                    print("  │  │  └─ (No states available for this combination)")
                                    total_no_data += 1
                                    continue

                                states = get_select_options(driver, "st_id")
                                if not states:
                                    print("  │  │  └─ (No states loaded)")
                                    continue
                                
                                for state in states:
                                    state_text = state["text"]
                                    print(f"  │  │  ├─ {state_text} ... ", end="", flush=True)

                                    fname = sanitize(f"{season_text}_{year_text}_{commodity_text}_{crop_text}_{state_text}")
                                    if fname in already_done:
                                        print("skipped")
                                        total_skipped += 1
                                        continue

                                    try:
                                        select_dropdown_and_stabilize(driver, "st_id", state["value"])

                                        table_status = click_submit_and_wait(driver)
                                        
                                        if table_status == "no_data":
                                            print("no data")
                                            total_no_data += 1
                                            continue
                                        elif table_status == "timeout":
                                            print("timed out loading results")
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
                                print(f"  │  └─ ERROR processing crop {crop_text}: {crop_err}")
                                total_errors += 1
                                continue

                    except Exception as commodity_err:
                        print(f"  └─ ERROR processing commodity {commodity_text}: {commodity_err}")
                        total_errors += 1
                        continue

            except Exception as combo_err:
                print(f"\n  Loop contextual alignment sync tracking break in {season_text} {year_text}, re-routing session workflow...")
                driver.get(URL)
                wait_for_page_load(driver)
                continue

except Exception as e:
    import traceback
    print("\n[CRITICAL RUN INTERRUPTION]")
    traceback.print_exc()

finally:
    print(f"\n{'='*60}")
    print(f"  Run complete")
    print(f"  Saved    : {total_saved}")
    print(f"  Skipped  : {total_skipped}")
    print(f"  No data  : {total_no_data}")
    print(f"  Errors   : {total_errors}")
    print(f"  Location : {DOWNLOAD_DIR}")
    print(f"{'='*60}")
    driver.quit()