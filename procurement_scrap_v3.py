import os
import re
import time
import logging

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
    ElementNotInteractableException,
)

# =========================================
# LOGGING SETUP
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

EXCLUDE_2026_27 = True
MAX_COMBO_RETRIES = 3

# =========================================
# DRIVER
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
    driver = webdriver.Chrome(options=options)
    driver.maximize_window()
    return driver

# =========================================
# HELPERS
# =========================================

def sanitize(text: str) -> str:
    return re.sub(r'[\/*?:"<>|]', "_", text.strip())


def _read_options(driver, select_id: str) -> list[dict]:
    el = driver.find_element(By.ID, select_id)
    if el.get_attribute("disabled") is not None:
        return []
    result = []
    for opt in Select(el).options:
        val = opt.get_attribute("value")
        txt = opt.text.strip()
        if val and val not in ("0", "") and txt:
            result.append({"value": val, "text": txt})
    return result


def _get_selected_value(driver, select_id: str) -> str | None:
    try:
        el = driver.find_element(By.ID, select_id)
        return Select(el).first_selected_option.get_attribute("value")
    except Exception:
        return None


def wait_for_dropdown_to_clear(driver, select_id: str, timeout: float = 8) -> bool:
    """Wait until dropdown has zero meaningful options (Angular cleared it)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if not _read_options(driver, select_id):
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def wait_for_dropdown_options(
    driver,
    select_id: str,
    timeout: float = 15,
    stable_rounds: int = 3,
    check_no_data_warning: bool = False,
) -> list[dict]:
    """
    Wait until dropdown options stabilise across `stable_rounds` consecutive
    0.2s reads. Also checks Angular's ".missing_field" warning if requested.
    """
    end = time.time() + timeout
    last: list[dict] | None = None
    hits = 0

    while time.time() < end:
        if check_no_data_warning:
            try:
                for w in driver.find_elements(By.CSS_SELECTOR, ".missing_field"):
                    if w.is_displayed() and "no state list found" in w.text.lower():
                        return []
            except Exception:
                pass

        try:
            snap = _read_options(driver, select_id)
        except (StaleElementReferenceException, NoSuchElementException):
            last, hits = None, 0
            time.sleep(0.2)
            continue

        if snap and snap == last:
            hits += 1
            if hits >= stable_rounds:
                return snap
        else:
            last, hits = snap, 0

        time.sleep(0.2)

    return last if last else []


def robust_select(
    driver,
    select_id: str,
    value: str,
    downstream_id: str | None = None,
    timeout: float = 15,
) -> bool:
    """
    Select `value` in `select_id`, confirm it stuck, then optionally wait for
    `downstream_id` to clear (proving Angular registered the change).
    """
    end = time.time() + timeout
    while time.time() < end:
        try:
            el = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.ID, select_id))
            )
            Select(el).select_by_value(value)
            time.sleep(0.15)
            if _get_selected_value(driver, select_id) != value:
                time.sleep(0.3)
                continue
            if downstream_id:
                wait_for_dropdown_to_clear(driver, downstream_id, timeout=5)
            return True
        except (StaleElementReferenceException, ElementNotInteractableException):
            time.sleep(0.3)
        except Exception:
            time.sleep(0.3)
    return False


def _get_result_signature(driver) -> str:
    """
    Returns a string that uniquely identifies the currently displayed result.
    Built from the result metadata band shown above the table:
      'Marketing Season : KMS  Marketing Year : 2025-2026  Commodity : Paddy  ...'
    Plus the State label shown in the results section.
    This lets us confirm the table belongs to the combo we just submitted.
    """
    try:
        # Grab the "to-bg mb-2" div that shows Season/Year/Commodity/CropType
        meta_els = driver.find_elements(By.CSS_SELECTOR, ".table-warp .to-bg .col-md-3")
        meta = " | ".join(e.text.strip() for e in meta_els if e.text.strip())

        # Grab the State label from the results section (col with "State :")
        state_els = driver.find_elements(
            By.CSS_SELECTOR, ".table-warp .row.mb-2 .col-sm-12.col-md-4.col-lg-6"
        )
        state_txt = " | ".join(e.text.strip() for e in state_els if e.text.strip())

        return f"{meta} || {state_txt}"
    except Exception:
        return ""


def _table_has_data_rows(driver) -> bool:
    """
    True only if tbody contains at least one real data row.
    Explicitly ignores tfoot (Grand Total) to prevent false positives.
    """
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        for row in rows:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) >= 2 and cells[1].text.strip():
                return True
    except Exception:
        pass
    return False


def click_submit_get_result(driver, expected_combo_label: str, timeout: float = 45) -> str:
    """
    Submits the form and waits for a FRESH result that matches `expected_combo_label`.

    The label is built from (season_text, year_text, commodity_text, crop_text, state_text)
    and compared against the metadata band Angular renders above the table.

    Returns: "ok" | "no_data" | "timeout"

    FIX: We no longer wipe tbody via JS (breaks Angular). Instead we:
      1. Record the pre-submit result signature.
      2. Click submit.
      3. Wait for the signature to CHANGE — this is the true "fresh result" gate.
      4. Then confirm tbody has actual data rows (not just tfoot).
    """
    pre_sig = _get_result_signature(driver)

    # Click submit
    try:
        btn = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']"))
        )
        driver.execute_script("arguments[0].click();", btn)
    except Exception as e:
        return f"submit_error:{e}"

    end = time.time() + timeout
    while time.time() < end:
        # Check for Angular no-data alert
        try:
            for alert in driver.find_elements(By.CSS_SELECTOR, ".alert-danger-msg"):
                if alert.is_displayed() and "no records found" in alert.text.lower():
                    return "no_data"
        except Exception:
            pass

        # Check if result signature changed from pre-submit state
        try:
            current_sig = _get_result_signature(driver)
            if current_sig and current_sig != pre_sig:
                # Signature changed — verify it's for our exact combo
                if expected_combo_label.lower() in current_sig.lower():
                    # Confirm tbody actually has data rows (not just tfoot)
                    if _table_has_data_rows(driver):
                        return "ok"
                    else:
                        # Sig changed but no rows yet — keep waiting
                        pass
                else:
                    # Sig changed but to wrong combo — page state mismatch
                    log.warning(f"    [sig mismatch] expected: {expected_combo_label!r}, got: {current_sig!r}")
                    return "sig_mismatch"
        except Exception:
            pass

        time.sleep(0.25)

    return "timeout"


def wait_for_download(directory: str, before: set, timeout: int = 90) -> str | None:
    end = time.time() + timeout
    while time.time() < end:
        current = set(os.listdir(directory))
        new_files = [
            f for f in (current - before)
            if f.endswith((".xlsx", ".xls", ".csv"))
            and not f.endswith(".crdownload")
        ]
        partials = [f for f in current if f.endswith(".crdownload")]
        if new_files and not partials:
            return os.path.join(directory, sorted(new_files)[-1])
        time.sleep(0.5)
    return None


def reload_and_wait(driver) -> bool:
    try:
        driver.get(URL)
        WebDriverWait(driver, 30).until(
            lambda d: len(_read_options(d, "m_s_id")) > 0
        )
        return True
    except Exception:
        return False


def navigate_to_crop(driver, season, year, commodity, crop) -> bool:
    """Full navigation from season → crop with downstream-clear gates at each step."""
    return (
        robust_select(driver, "m_s_id",    season["value"],    downstream_id="m_year")
        and robust_select(driver, "m_year",    year["value"],      downstream_id="comdty_id")
        and robust_select(driver, "comdty_id", commodity["value"], downstream_id="c_type_id")
        and robust_select(driver, "c_type_id", crop["value"],      downstream_id="st_id")
    )

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

    # Confirm state option still present after prior navigation
    states_now = wait_for_dropdown_options(driver, "st_id", timeout=12, stable_rounds=3)
    if not any(s["value"] == state["value"] for s in states_now):
        return "error:state option disappeared"

    if not robust_select(driver, "st_id", state["value"]):
        return "error:state select failed"

    # Build a matchable label from the metadata Angular will render
    # The page shows:  "Marketing Season : KMS  Marketing Year : 2025-2026
    #                   Commodity : Paddy  Crop Type : Kharif"  + "State : ASSAM"
    expected_label = state["text"]   # matching just the state name is sufficient & robust

    status = click_submit_get_result(driver, expected_label)

    if status == "no_data":
        return "no_data"
    if status != "ok":
        return f"timeout_or_error:{status}"

    # Download
    before = set(os.listdir(DOWNLOAD_DIR))
    try:
        xl_btn = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button[aria-label='Download Report in Excel Format']")
            )
        )
        driver.execute_script("arguments[0].click();", xl_btn)
    except Exception as e:
        return f"error:excel button: {e}"

    downloaded = wait_for_download(DOWNLOAD_DIR, before)
    if not downloaded:
        return "error:download timeout"

    ext = os.path.splitext(downloaded)[1]
    target = os.path.join(DOWNLOAD_DIR, fname + ext)
    if os.path.exists(target):
        os.remove(target)
    os.rename(downloaded, target)
    already_done.add(fname)
    return "saved"

# =========================================
# MAIN ENGINE
# =========================================

MARKETING_SEASONS = [
    {"value": "1", "text": "KMS"},
    {"value": "2", "text": "RMS"},
]
MARKETING_YEARS = [
    {"value": "2026-2027", "text": "2026-2027"},
    {"value": "2025-2026", "text": "2025-2026"},
    {"value": "2024-2025", "text": "2024-2025"},
    {"value": "2023-2024", "text": "2023-2024"},
    {"value": "2022-2023", "text": "2022-2023"},
    {"value": "2021-2022", "text": "2021-2022"},
]
if EXCLUDE_2026_27:
    MARKETING_YEARS = [y for y in MARKETING_YEARS if y["value"] != "2026-2027"]


def main():
    already_done = {
        os.path.splitext(f)[0]
        for f in os.listdir(DOWNLOAD_DIR)
        if f.endswith((".xlsx", ".xls", ".csv"))
    }
    total_saved = total_skipped = total_no_data = total_errors = 0

    log.info("=" * 60)
    log.info("  Procurement Scraper — v4 (Signature-Verified Table Build)")
    log.info(f"  Destination : {DOWNLOAD_DIR}")
    log.info(f"  Already done: {len(already_done)} files")
    log.info("=" * 60)

    driver = make_driver()

    try:
        if not reload_and_wait(driver):
            log.error("Initial page load failed. Aborting.")
            return

        for season in MARKETING_SEASONS:
            for year in MARKETING_YEARS:
                log.info(f"\n► {season['text']}  {year['text']}")

                if not reload_and_wait(driver):
                    log.warning("  Page reload failed; skipping year block.")
                    total_errors += 1
                    continue

                if not robust_select(driver, "m_s_id", season["value"], downstream_id="m_year"):
                    log.warning("  Season select failed; skipping.")
                    total_errors += 1
                    continue
                if not robust_select(driver, "m_year", year["value"], downstream_id="comdty_id"):
                    log.warning("  Year select failed; skipping.")
                    total_errors += 1
                    continue

                commodities = wait_for_dropdown_options(driver, "comdty_id", timeout=12)
                if not commodities:
                    log.info("  (No commodities)")
                    continue
                log.info(f"  {len(commodities)} commodities")

                for commodity in commodities:
                    log.info(f"  ┌─ {commodity['text']}")

                    if not reload_and_wait(driver):
                        log.warning("  │  Reload failed; skipping commodity.")
                        total_errors += 1
                        continue

                    if not robust_select(driver, "m_s_id", season["value"], downstream_id="m_year"):
                        log.warning("  │  Season re-select failed.")
                        total_errors += 1
                        continue
                    if not robust_select(driver, "m_year", year["value"], downstream_id="comdty_id"):
                        log.warning("  │  Year re-select failed.")
                        total_errors += 1
                        continue
                    if not robust_select(driver, "comdty_id", commodity["value"], downstream_id="c_type_id"):
                        log.warning("  │  Commodity select failed.")
                        total_errors += 1
                        continue

                    crop_types = wait_for_dropdown_options(driver, "c_type_id", timeout=10)
                    if not crop_types:
                        log.info("  │  └─ (No crop types)")
                        continue

                    for crop in crop_types:
                        log.info(f"  │  ├─ {crop['text']}")

                        if not robust_select(driver, "c_type_id", crop["value"], downstream_id="st_id"):
                            log.warning("  │  │  Crop select failed.")
                            total_errors += 1
                            continue

                        states = wait_for_dropdown_options(
                            driver, "st_id",
                            timeout=12, stable_rounds=3,
                            check_no_data_warning=True,
                        )
                        if not states:
                            log.info("  │  │  └─ (No states)")
                            total_no_data += 1
                            continue
                        log.info(f"  │  │   {len(states)} states")

                        for state in states:
                            label = f"  │  │  ├─ {state['text']}"
                            result = "error:not_attempted"

                            for attempt in range(1, MAX_COMBO_RETRIES + 1):
                                if attempt > 1:
                                    log.info(f"  │  │  │  (retry {attempt}/{MAX_COMBO_RETRIES})")
                                    # Full reload + re-navigation to this crop on retry
                                    if not reload_and_wait(driver):
                                        result = "error:reload failed"
                                        break
                                    if not navigate_to_crop(driver, season, year, commodity, crop):
                                        result = "error:navigation failed on retry"
                                        break
                                    # Re-verify state still exists after re-navigation
                                    retry_states = wait_for_dropdown_options(
                                        driver, "st_id", timeout=12, stable_rounds=3
                                    )
                                    if not any(s["value"] == state["value"] for s in retry_states):
                                        result = "error:state gone after retry nav"
                                        break

                                result = process_combination(
                                    driver, season, year, commodity, crop, state, already_done
                                )

                                if result in ("saved", "skipped", "no_data"):
                                    break
                                if result == "error:state option disappeared":
                                    break  # genuinely absent, don't retry

                            if result == "saved":
                                log.info(f"{label} ... saved")
                                total_saved += 1
                            elif result == "skipped":
                                log.info(f"{label} ... skipped")
                                total_skipped += 1
                            elif result == "no_data":
                                log.info(f"{label} ... no data")
                                total_no_data += 1
                            else:
                                log.warning(f"{label} ... {result}")
                                total_errors += 1

    except Exception as critical:
        log.exception(f"[CRITICAL] {critical}")

    finally:
        log.info("\n" + "=" * 60)
        log.info(
            f"  Run complete\n"
            f"  Saved  : {total_saved}\n"
            f"  Skipped: {total_skipped}\n"
            f"  Empty  : {total_no_data}\n"
            f"  Errors : {total_errors}"
        )
        log.info("=" * 60)
        driver.quit()


if __name__ == "__main__":
    main()