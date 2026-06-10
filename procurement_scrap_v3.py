import os
import time
import logging
import shutil
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, TimeoutException

# ==============================================================================
# LOGGING CONFIGURATION
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)s │ %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ==============================================================================
# CONFIGURATION AND GLOBAL CONSTANTS
# ==============================================================================
URL = "https://cfpp.nic.in/#/report/3/proc_procuring_agency/"
DOWNLOAD_DIR = os.path.abspath("procurement_data")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Dynamic timeout windows (in seconds)
UI_INTERACTION_TIMEOUT = 15
SERVER_RESPONSE_TIMEOUT = 45
DOWNLOAD_SETTLE_TIMEOUT = 60

# Static structural layers
SEASONS = [{"value": "1", "text": "KMS"}, {"value": "2", "text": "RMS"}]
YEARS = [{"value": f"{y}-{y+1}", "text": f"{y}-{y+1}"} for y in range(2025, 2020, -1)]

# ==============================================================================
# BROWSER LIFECYCLE MANAGEMENT
# ==============================================================================
def start_fresh_browser():
    """Launches a clean Chrome instance with isolated configurations."""
    log.info("♻️ Launching clean Chrome browser instance...")
    options = webdriver.ChromeOptions()
    options.add_experimental_option("prefs", {
        "download.default_directory": DOWNLOAD_DIR,
        "download.prompt_for_download": False,
        "profile.default_content_setting_values.automatic_downloads": 1,
        "safebrowsing.enabled": True
    })
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(SERVER_RESPONSE_TIMEOUT)
    return driver

def shutdown_browser(driver):
    """Ensures absolute cleanup of active driver processes."""
    if driver:
        try:
            driver.quit()
        except Exception:
            pass

# ==============================================================================
# ROBUST UI NAVIGATIONAL PRIMITIVES
# ==============================================================================
def safely_extract_options(driver, select_id):
    """Extracts valid options from a dropdown, ensuring options are populated."""
    end_time = time.time() + UI_INTERACTION_TIMEOUT
    while time.time() < end_time:
        try:
            element = WebDriverWait(driver, 3).until(
                EC.presence_of_element_located((By.ID, select_id))
            )
            select_obj = Select(element)
            options = [
                {"value": o.get_attribute("value"), "text": o.text.strip()}
                for o in select_obj.options 
                if o.get_attribute("value") not in ("", "0") and o.text.strip()
            ]
            if options:
                return options
        except Exception:
            pass
        time.sleep(0.5)
    return []

def execute_dropdown_selection(driver, select_id, target_text):
    """Selects an option by visible text, confirming DOM stability."""
    end_time = time.time() + UI_INTERACTION_TIMEOUT
    while time.time() < end_time:
        try:
            element = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.ID, select_id))
            )
            select_obj = Select(element)
            
            # Check current selection to avoid redundant changes
            try:
                if select_obj.first_selected_option.text.strip() == target_text:
                    return True
            except Exception:
                pass
                
            select_obj.select_by_visible_text(target_text)
            time.sleep(0.5)
            return True
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutException(f"Failed to settle selection on dropdown element: ID {select_id}")

def rebuild_navigation_path(driver, season, year, commodity, crop):
    """Walks down the selection tree to restore context after a hard restart."""
    driver.get(URL)
    execute_dropdown_selection(driver, "m_s_id", season)
    execute_dropdown_selection(driver, "m_year", year)
    execute_dropdown_selection(driver, "comdty_id", commodity)
    execute_dropdown_selection(driver, "c_type_id", crop)

# ==============================================================================
# SYSTEM RUN ENGINE
# ==============================================================================
def main():
    driver = start_fresh_browser()
    
    try:
        driver.get(URL)
        log.info("Successfully connected to portal gateway.")
    except Exception as e:
        log.error(f"Initial connection handshake failed: {e}. Restarting cycle...")
        shutdown_browser(driver)
        driver = start_fresh_browser()
        driver.get(URL)

    for s_opt in SEASONS:
        for y_opt in YEARS:
            log.info(f"\n► Processing Block: {s_opt['text']} │ {y_opt['text']}")
            
            # Layer 1 & 2 Restructuring / Navigation Guard
            while True:
                try:
                    execute_dropdown_selection(driver, "m_s_id", s_opt["text"])
                    execute_dropdown_selection(driver, "m_year", y_opt["text"])
                    commodities = safely_extract_options(driver, "comdty_id")
                    break
                except Exception:
                    log.warning("Server interaction failure at base block layer. Re-launching URL context...")
                    shutdown_browser(driver)
                    driver = start_fresh_browser()
                    driver.get(URL)

            for comp in commodities:
                # Layer 3 Navigation Guard
                while True:
                    try:
                        execute_dropdown_selection(driver, "m_s_id", s_opt["text"])
                        execute_dropdown_selection(driver, "m_year", y_opt["text"])
                        execute_dropdown_selection(driver, "comdty_id", comp["text"])
                        crops = safely_extract_options(driver, "c_type_id")
                        break
                    except Exception:
                        log.warning(f"Hang detected at Commodity [{comp['text']}]. Executing structural reset...")
                        shutdown_browser(driver)
                        driver = start_fresh_browser()
                        rebuild_navigation_path(driver, s_opt["text"], y_opt["text"], "", "")

                for cr in crops:
                    log.info(f"  ┌─ Core Matrix: {comp['text']} ──> {cr['text']}")
                    
                    # Layer 4 Navigation Guard
                    while True:
                        try:
                            execute_dropdown_selection(driver, "c_type_id", cr["text"])
                            states = safely_extract_options(driver, "st_id")
                            break
                        except Exception:
                            log.warning(f"Hang detected at Category Segment [{cr['text']}]. Executing structural reset...")
                            shutdown_browser(driver)
                            driver = start_fresh_browser()
                            rebuild_navigation_path(driver, s_opt["text"], y_opt["text"], comp["text"], "")

                    for st in states:
                        # Normalize names to establish absolute persistent file keys
                        safe_s = s_opt['text'].replace("/", "_").replace(" ", "_")
                        safe_y = y_opt['text'].replace("/", "_").replace(" ", "_")
                        safe_comp = comp['text'].replace("/", "_").replace(" ", "_")
                        safe_cr = cr['text'].replace("/", "_").replace(" ", "_")
                        safe_st = st['text'].replace("/", "_").replace(" ", "_")
                        
                        base_filename = f"{safe_s}_{safe_y}_{safe_comp}_{safe_cr}_{safe_st}"
                        xlsx_path = os.path.join(DOWNLOAD_DIR, f"{base_filename}.xlsx")
                        nodata_path = os.path.join(DOWNLOAD_DIR, f"{base_filename}.nodata")

                        # Permanent Checkpoint Lookups: If either file exists, it's 100% processed
                        if os.path.exists(xlsx_path):
                            log.info(f"  │  ├─ {st['text']} ... [SKIPPED] (Dataset already exists local)")
                            continue
                        if os.path.exists(nodata_path):
                            log.info(f"  │  ├─ {st['text']} ... [SKIPPED] (Confirmed No-Data Matrix Record)")
                            continue

                        # Execution loop for single cell processing
                        while True:
                            try:
                                execute_dropdown_selection(driver, "st_id", st["text"])
                                
                                # Intercept and trigger submission action
                                submit_btn = WebDriverWait(driver, UI_INTERACTION_TIMEOUT).until(
                                    EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']"))
                                )
                                driver.execute_script("arguments[0].click();", submit_btn)

                                # DYNAMIC CONDITIONAL WAIT FOR RESULT ELEMENT PATHS
                                # Monitor DOM loop structure for either explicit state presentation path
                                end_wait = time.time() + SERVER_RESPONSE_TIMEOUT
                                verified_state = None
                                
                                while time.time() < end_wait:
                                    # Scenario A: Validate No Records Element
                                    no_records = driver.find_elements(By.CSS_SELECTOR, "h3.alert-danger-msg")
                                    if no_records and any(r.is_displayed() and "no records found" in r.text.lower() for r in no_records):
                                        verified_state = "NO_DATA"
                                        break
                                    
                                    # Scenario B: Validate Dynamic Table Warp Data Container Structure
                                    data_table = driver.find_elements(By.CSS_SELECTOR, ".table-warp table#content tbody tr")
                                    if data_table and any(row.is_displayed() for row in data_table):
                                        verified_state = "DATA_PRESENT"
                                        break
                                        
                                    time.sleep(0.5)

                                if not verified_state:
                                    raise TimeoutException("Server failed to yield an explicit UI state path.")

                                # Handle State Condition A
                                if verified_state == "NO_DATA":
                                    with open(nodata_path, "w") as f:
                                        f.write("No data records match structural verification query.")
                                    log.info(f"  │  ├─ {st['text']} ... Verified: [No Records Found]")
                                    break

                                # Handle State Condition B
                                elif verified_state == "DATA_PRESENT":
                                    excel_btn = WebDriverWait(driver, UI_INTERACTION_TIMEOUT).until(
                                        EC.element_to_be_clickable((By.CSS_SELECTOR, "button[aria-label='Download Report in Excel Format']"))
                                    )
                                    
                                    before_files = set(os.listdir(DOWNLOAD_DIR))
                                    driver.execute_script("arguments[0].click();", excel_btn)

                                    # Settle file-write stream cleanly on local filesystem
                                    downloaded_filename = None
                                    end_download_wait = time.time() + DOWNLOAD_SETTLE_TIMEOUT
                                    
                                    while time.time() < end_download_wait:
                                        time.sleep(0.5)
                                        current_files = set(os.listdir(DOWNLOAD_DIR))
                                        diff = current_files - before_files
                                        valid = [f for f in diff if f.endswith((".xlsx", ".xls")) and not f.endswith(".crdownload")]
                                        if valid:
                                            downloaded_filename = valid[0]
                                            break
                                            
                                    if not downloaded_filename:
                                        raise TimeoutError("Excel file generation pipeline did not complete transfer.")

                                    # Finalize data security mapping transformation
                                    shutil.move(
                                        os.path.join(DOWNLOAD_DIR, downloaded_filename), 
                                        xlsx_path
                                    )
                                    log.info(f"  │  ├─ {st['text']} ... Saved successfully")
                                    break

                            except Exception as state_error:
                                log.warning(f"  │  ├─ {st['text']} ... [TIMEOUT / SERVER HANG]: ({state_error}). Recalibrating workflow engine...")
                                shutdown_browser(driver)
                                driver = start_fresh_browser()
                                rebuild_navigation_path(driver, s_opt["text"], y_opt["text"], comp["text"], cr["text"])

    shutdown_browser(driver)
    log.info("\nPipeline execution sequence completed successfully. All data nodes finalized.")

if __name__ == "__main__":
    main()