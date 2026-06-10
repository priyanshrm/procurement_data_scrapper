import os
import re
import time
import logging
import shutil

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
    ElementNotInteractableException,
    WebDriverException,
)

# =========================================
# LOGGING
# =========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# =========================================
# CONFIGURATION
# =========================================

URL = "https://cfpp.nic.in/#/report/3/proc_procuring_agency/"
DOWNLOAD_DIR = os.path.abspath("procurement_data")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

EXCLUDE_2026_27      = True
MAX_COMBO_RETRIES   = 3
PAGE_LOAD_TIMEOUT   = 45   
SUBMIT_TIMEOUT      = 45   

# Restart Chrome proactively after this many downloads to prevent memory crash
DRIVER_RESTART_EVERY = 10

# =========================================
# DRIVER MANAGEMENT
# =========================================

def make_driver() -> webdriver.Chrome:
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
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--metrics-recording-only")
    options.add_argument("--mute-audio")
    options.add_argument("--no-first-run")
    options.add_argument("--safebrowsing-disable-auto-update")
    options.add_argument("--js-flags=--max-old-space-size=512")
    
    driver = webdriver.Chrome(options=options)
    driver.maximize_window()
    return driver


def safe_quit(driver):
    """Quit driver safely, swallowing any OS or communication errors."""
    try:
        driver.quit()
    except Exception:
        pass


def is_driver_alive(driver) -> bool:
    """Check if the ChromeDriver process is still healthy and responding."""
    try:
        _ = driver.title  
        return True
    except Exception:
        return False


def restart_driver(driver) -> webdriver.Chrome:
    """Kill old driver instance cleanly and spawn a brand-new process."""
    log.info("  ♻  Restarting Chrome driver…")
    safe_quit(driver)
    time.sleep(2)
    new_driver = make_driver()
    log.info("  ♻  Chrome driver restarted.")
    return new_driver

# =========================================
# DOM HELPERS & STALE-SAFE IMPLEMENTATIONS
# =========================================

def sanitize(text: str) -> str:
    return re.sub(r'[\/*?:"<>|]', "_", text.strip())


def _read_options(driver, select_id: str) -> list[dict]:
    """Return all non-placeholder options. Built-in stale retry logic."""
    for _ in range(3):
        try:
            el = driver.find_element(By.ID, select_id)
            if el.get_attribute("disabled") is not None:
                return []
            
            result = []
            options = Select(el).options
            for opt in options:
                val = opt.get_attribute("value")
                txt = opt.text.strip()
                if val and val not in ("0", "") and txt:
                    result.append({"value": val, "text": txt})
            return result
        except (StaleElementReferenceException, NoSuchElementException):
            time.sleep(0.2)
    return []


def _get_selected_value(driver, select_id: str) -> str | None:
    try:
        el = driver.find_element(By.ID, select_id)
        return Select(el).first_selected_option.get_attribute("value")
    except Exception:
        return None

# =========================================
# WAIT PRIMITIVES
# =========================================

def wait_for_dropdown_to_clear(driver, select_id: str, timeout: float = 10) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if not _read_options(driver, select_id):
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def wait_for_dropdown_options(
    driver,
    select_id: str,
    timeout: float = 15,
    stable_rounds: int = 3,
    check_no_data_warning: bool = False,
) -> list[dict]:
    end = time.time() + timeout
    last: list[dict] | None = None
    hits = 0
    empty_since: float | None = None

    while time.time() < end:
        try:
            snap = _read_options(driver, select_id)
        except Exception:
            last, hits = None, 0
            empty_since = None
            time.sleep(0.2)
            continue

        if snap:
            empty_since = None
            if snap == last:
                hits += 1
                if hits >= stable_rounds:
                    return snap
            else:
                last, hits = snap, 0
        else:
            if empty_since is None:
                empty_since = time.time()
            last, hits = [], 0
            if check_no_data_warning and (time.time() - empty_since) >= 2.0:
                try:
                    for w in driver.find_elements(By.CSS_SELECTOR, ".missing_field"):
                        if w.is_displayed() and "no state list found" in w.text.lower():
                            return []
                except Exception:
                    pass

        time.sleep(0.25)
    return last if last else []

# =========================================
# RELOAD + HARD RESET
# =========================================

def reload_and_wait(driver) -> bool:
    try:
        driver.get(URL)
        WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
            lambda d: "CFPP" in d.title or "cfpp" in d.title.lower()
        )

        def season_ready(d):
            try:
                return len(_read_options(d, "m_s_id")) >= 2
            except Exception:
                return False
                
        WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(season_ready)

        driver.execute_script(
            "try { window.sessionStorage.clear(); } catch(e) {}"
            "try { window.localStorage.clear();   } catch(e) {}"
        )
        driver.get(URL)
        WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
            lambda d: "CFPP" in d.title or "cfpp" in d.title.lower()
        )
        WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(season_ready)

        try:
            WebDriverWait(driver, 10).until(
                lambda d: d.find_element(By.ID, "comdty_id").get_attribute("disabled") is None
            )
        except Exception:
            pass

        return len(_read_options(driver, "m_s_id")) >= 2

    except Exception as e:
        log.warning(f"  reload_and_wait error: {e}")
        return False

# =========================================
# SELECTION LOGIC
# =========================================

def robust_select(
    driver,
    select_id: str,
    value: str,
    downstream_id: str | None = None,
    timeout: float = 15,
) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            el = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((By.ID, select_id))
            )
            Select(el).select_by_value(value)
            time.sleep(0.2)
            if _get_selected_value(driver, select_id) != value:
                continue
            if downstream_id:
                wait_for_dropdown_to_clear(driver, downstream_id, timeout=6)
            return True
        except (StaleElementReferenceException, ElementNotInteractableException):
            time.sleep(0.3)
        except WebDriverException:
            raise   
        except Exception:
            time.sleep(0.3)
    return False


def navigate_to_crop(driver, season, year, commodity, crop) -> bool:
    return (
        robust_select(driver, "m_s_id",    season["value"],    downstream_id=None)
        and robust_select(driver, "m_year",    year["value"],      downstream_id="comdty_id")
        and robust_select(driver, "comdty_id", commodity["value"], downstream_id="c_type_id")
        and robust_select(driver, "c_type_id", crop["value"],      downstream_id="st_id")
    )

# =========================================
# RESULT VERIFICATION
# =========================================

def _get_result_signature(driver) -> str:
    """Generates state identity text. Safeguarded against elements changing mid-parse."""
    try:
        meta_els = driver.find_elements(By.CSS_SELECTOR, ".table-warp .to-bg .col-md-3")
        meta = " | ".join(e.text.strip() for e in meta_els if e.text.strip())
        state_els = driver.find_elements(
            By.CSS_SELECTOR, ".table-warp .row.mb-2 .col-sm-12.col-md-4.col-lg-6"
        )
        state_txt = " | ".join(e.text.strip() for e in state_els if e.text.strip())
        return f"{meta} || {state_txt}"
    except (StaleElementReferenceException, NoSuchElementException):
        return ""


def _table_has_data_rows(driver) -> bool:
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        for row in rows:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) >= 2 and cells[1].text.strip():
                return True
    except Exception:
        pass
    return False


def click_submit_get_result(driver, expected_state_name: str) -> str:
    pre_sig = _get_result_signature(driver)

    try:
        btn = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']"))
        )
        driver.execute_script("arguments[0].click();", btn)
    except Exception as e:
        return f"submit_error:{e}"

    end = time.time() + SUBMIT_TIMEOUT
    sig_changed = False

    while time.time() < end:
        if not sig_changed:
            sig = _get_result_signature(driver)
            if sig and sig != pre_sig:
                sig_changed = True
                if expected_state_name.lower() not in sig.lower():
                    log.warning(f"    sig_mismatch: expected {expected_state_name!r} not in {sig!r}")
                    return "sig_mismatch"
            time.sleep(0.25)
            continue

        try:
            for alert in driver.find_elements(By.CSS_SELECTOR, ".alert-danger-msg"):
                if alert.is_displayed() and "no records found" in alert.text.lower():
                    return "no_data"
        except Exception:
            pass

        if _table_has_data_rows(driver):
            return "ok"

        time.sleep(0.25)

    return "timeout"

# =========================================
# DOWNLOAD MANAGEMENT
# =========================================

def wait_for_download(directory: str, before: set, timeout: int = 90) -> str | None:
    end = time.time() + timeout
    while time.time() < end:
        current = set(os.listdir(directory))
        new_files = [
            f for f in (current - before)
            if f.endswith((".xlsx", ".xls", ".csv"))
            and not f.endswith(".crdownload")
            and not f.startswith(".com.google.Chrome")
        ]
        if new_files and not any(f.endswith(".crdownload") for f in current):
            return os.path.join(directory, sorted(new_files)[-1])
        time.sleep(0.5)
    return None

# =========================================
# COMBO PROCESSOR
# =========================================

def process_combination(
    driver,
    season: dict,
    year: dict,
    commodity: dict,
    crop: dict,
    state: dict,
    already_done: set,
) -> str:
    fname = sanitize(
        f"{season['text']}_{year['text']}_{commodity['text']}_{crop['text']}_{state['text']}"
    )
    if fname in already_done:
        return "skipped"

    states_now = wait_for_dropdown_options(driver, "st_id", timeout=12, stable_rounds=3)
    if not any(s["value"] == state["value"] for s in states_now):
        return "error:state option disappeared"

    if not robust_select(driver, "st_id", state["value"]):
        return "error:state select failed"

    status = click_submit_get_result(driver, state["text"])
    if status == "no_data":
        return "no_data"
    if status != "ok":
        return f"timeout_or_error:{status}"

    # Explicitly wait until download button is completely actionable rather than raw sleeping
    try:
        xl_btn = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button[aria-label='Download Report in Excel Format']")
            )
        )
        before = set(os.listdir(DOWNLOAD_DIR))
        driver.execute_script("arguments[0].click();", xl_btn)
    except Exception as e:
        return f"error:excel button: {e}"

    downloaded = wait_for_download(DOWNLOAD_DIR, before)
    if not downloaded:
        return "error:download timeout"

    ext = os.path.splitext(downloaded)[1]
    target = os.path.join(DOWNLOAD_DIR, fname + ext)
    
    try:
        if os.path.exists(target):
            os.remove(target)
        shutil.move(downloaded, target)
    except Exception as file_err:
        return f"error:file_io_failed:{file_err}"
        
    already_done.add(fname)
    return "saved"

# =========================================
# PER-CROP STATE LOOP
# =========================================
# =========================================
# PER-CROP STATE LOOP
# =========================================

def process_states_for_crop(
    driver_ref: list,           
    season: dict,
    year: dict,
    commodity: dict,
    crop: dict,
    already_done: set,
    download_counter: list,     
) -> tuple[int, int, int, int]:
    saved = skipped = no_data = errors = 0

    def driver():
        return driver_ref[0]

    def _hard_restart_and_navigate() -> bool:
        """Forces a complete browser restart and navigates back to the current crop."""
        driver_ref[0] = restart_driver(driver())
        if not reload_and_wait(driver()):
            return False
        return navigate_to_crop(driver(), season, year, commodity, crop)

    def _navigate_to_this_crop() -> bool:
        if not is_driver_alive(driver()):
            return _hard_restart_and_navigate()
        if not reload_and_wait(driver()):
            return False
        return navigate_to_crop(driver(), season, year, commodity, crop)

    def _proactive_restart_if_due() -> bool:
        if download_counter[0] > 0 and download_counter[0] % DRIVER_RESTART_EVERY == 0:
            log.info(f"  ♻  Proactive restart triggered at threshold ({download_counter[0]} downloads)…")
            return _hard_restart_and_navigate()
        return True

    states = wait_for_dropdown_options(
        driver(), "st_id",
        timeout=15, stable_rounds=3,
        check_no_data_warning=True,
    )
    if not states:
        log.info("  │  │  └─ (No active states structural data found)")
        return 0, 0, 1, 0

    log.info(f"  │  │   {len(states)} states structural tracks isolated.")

    for state in states:
        label = f"  │  │  ├─ {state['text']}"
        result = "error:not_attempted"

        for attempt in range(1, MAX_COMBO_RETRIES + 1):
            if attempt > 1:
                log.warning(f"  │  │  │  (Failure detected. Hard restarting Chrome and retrying {attempt}/{MAX_COMBO_RETRIES})")
                
                # Nuke Chrome, restart, and walk back down the exact menu path
                if not _hard_restart_and_navigate():
                    result = "error:navigation_failed_after_hard_restart"
                    break
                
                # Verify the state dropdown is still valid after restart
                retry_states = wait_for_dropdown_options(driver(), "st_id", timeout=15, stable_rounds=3)
                if not any(s["value"] == state["value"] for s in retry_states):
                    result = "error:state_missing_after_restart"
                    break

            try:
                result = process_combination(driver(), season, year, commodity, crop, state, already_done)
            except WebDriverException as wde:
                log.warning(f"  │  │  WebDriver connection broken contextually: {wde}")
                result = "error:driver_crash_event"

            # If it succeeded, or there was legitimately no data/it was skipped, break out of the retry loop
            if result in ("saved", "skipped", "no_data", "error:state option disappeared"):
                break

        # Bookkeeping 
        if result == "saved":
            log.info(f"{label} ... saved")
            saved += 1
            download_counter[0] += 1
            if not _proactive_restart_if_due():
                errors += 1
                break
            if not _navigate_to_this_crop():
                errors += 1
                break
        elif result == "skipped":
            log.info(f"{label} ... skipped")
            skipped += 1
            if not _navigate_to_this_crop():
                errors += 1
                break
        elif result == "no_data":
            log.info(f"{label} ... no data")
            no_data += 1
            if not _navigate_to_this_crop():
                errors += 1
                break
        else:
            log.warning(f"{label} ... {result} (Max retries reached)")
            errors += 1
            # If a state completely failed after all max retries, do a hard reset before trying the next state in the loop
            if not _hard_restart_and_navigate():
                break

    return saved, skipped, no_data, errors

# =========================================
# MAIN CONTROL ENGINE
# =========================================

MARKETING_SEASONS = [
    {"value": "1", "text": "KMS"},
    {"value": "2", "text": "RMS"},
]
MARKETING_YEARS = [
    # {"value": "2026-2027", "text": "2026-2027"},
    # {"value": "2025-2026", "text": "2025-2026"},
    # {"value": "2024-2025", "text": "2024-2025"},
    # {"value": "2023-2024", "text": "2023-2024"},
    # {"value": "2022-2023", "text": "2022-2023"},
    {"value": "2021-2022", "text": "2021-2022"},
]
if EXCLUDE_2026_27:
    MARKETING_YEARS = [y for y in MARKETING_YEARS if y["value"] != "2026-2027"]


def _reload_with_driver(driver_ref: list) -> bool:
    if not is_driver_alive(driver_ref[0]):
        log.warning("  Driver context unreachable during cycle step — launching recovery…")
        driver_ref[0] = restart_driver(driver_ref[0])
    return reload_and_wait(driver_ref[0])


def main():
    already_done = {
        os.path.splitext(f)[0]
        for f in os.listdir(DOWNLOAD_DIR)
        if f.endswith((".xlsx", ".xls", ".csv"))
    }
    total_saved = total_skipped = total_no_data = total_errors = 0
    download_counter = [0]  

    log.info("=" * 60)
    log.info("  Procurement Automation Engine — Stable Release")
    log.info(f"  Target File Store : {DOWNLOAD_DIR}")
    log.info(f"  Pre-existing logs : {len(already_done)} datasets isolated.")
    log.info("=" * 60)

    driver_ref = [make_driver()]

    try:
        log.info("Connecting to target server portal...")
        if not reload_and_wait(driver_ref[0]):
            log.error("Portal gateway handshake timeout. Verify secure connection layout.")
            return
        log.info("Gateway ready.")

        for season in MARKETING_SEASONS:
            for year in MARKETING_YEARS:
                log.info(f"\n► Processing Block: {season['text']}  {year['text']}")

                if not _reload_with_driver(driver_ref):
                    total_errors += 1
                    continue

                if not robust_select(driver_ref[0], "m_s_id", season["value"]) or \
                   not robust_select(driver_ref[0], "m_year", year["value"], downstream_id="comdty_id"):
                    total_errors += 1
                    continue

                commodities = wait_for_dropdown_options(driver_ref[0], "comdty_id", timeout=15)
                if not commodities:
                    continue

                for commodity in commodities:
                    log.info(f"  ┌─ Core Matrix: {commodity['text']}")

                    if not _reload_with_driver(driver_ref):
                        total_errors += 1
                        continue

                    if not robust_select(driver_ref[0], "m_s_id", season["value"]) or \
                       not robust_select(driver_ref[0], "m_year", year["value"], downstream_id="comdty_id") or \
                       not robust_select(driver_ref[0], "comdty_id", commodity["value"], downstream_id="c_type_id"):
                        total_errors += 1
                        continue

                    crop_types = wait_for_dropdown_options(driver_ref[0], "c_type_id", timeout=12)
                    if not crop_types:
                        continue

                    for crop in crop_types:
                        log.info(f"  │  ├─ Category Segment: {crop['text']}")

                        if not robust_select(driver_ref[0], "c_type_id", crop["value"], downstream_id="st_id"):
                            total_errors += 1
                            continue

                        s, sk, nd, er = process_states_for_crop(
                            driver_ref, season, year, commodity, crop,
                            already_done, download_counter,
                        )
                        total_saved   += s
                        total_skipped += sk
                        total_no_data += nd
                        total_errors  += er

                        # Post-crop processing reset clean pass
                        if not _reload_with_driver(driver_ref):
                            break
                        robust_select(driver_ref[0], "m_s_id", season["value"])
                        robust_select(driver_ref[0], "m_year", year["value"], downstream_id="comdty_id")
                        robust_select(driver_ref[0], "comdty_id", commodity["value"], downstream_id="c_type_id")

    except Exception as critical:
        log.exception(f"[CRITICAL APPLICATION INTERRUPTION]: {critical}")

    finally:
        log.info("\n" + "=" * 60)
        log.info(
            f"  Execution Cycle Complete\n"
            f"  Successful extractions : {total_saved}\n"
            f"  Identified logs skipped: {total_skipped}\n"
            f"  Zero-entry arrays logs : {total_no_data}\n"
            f"  Exceptions handled     : {total_errors}\n"
            f"  Aggregation pipeline totals: {download_counter[0]}"
        )
        log.info("=" * 60)
        safe_quit(driver_ref[0])


if __name__ == "__main__":
    main()