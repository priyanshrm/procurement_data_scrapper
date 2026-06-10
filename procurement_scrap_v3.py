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

# How many times to retry a failed combination before giving up
MAX_COMBO_RETRIES = 3

# =========================================
# DRIVER FACTORY  (re-used on hard resets)
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
    """Read all non-placeholder options from a <select> element.
    Returns [] if the element is disabled or has no meaningful options.
    Raises StaleElementReferenceException / NoSuchElementException — callers handle.
    """
    el = driver.find_element(By.ID, select_id)
    if el.get_attribute("disabled") is not None:
        return []
    sel = Select(el)
    result = []
    for opt in sel.options:
        val = opt.get_attribute("value")
        txt = opt.text.strip()
        if val and val not in ("0", "") and txt:
            result.append({"value": val, "text": txt})
    return result


def _get_selected_value(driver, select_id: str) -> str | None:
    """Return currently selected value, or None on any DOM error."""
    try:
        el = driver.find_element(By.ID, select_id)
        return Select(el).first_selected_option.get_attribute("value")
    except Exception:
        return None


def wait_for_dropdown_options(
    driver,
    select_id: str,
    timeout: float = 15,
    stable_rounds: int = 3,
    check_no_data_warning: bool = False,
) -> list[dict]:
    """
    Waits until the dropdown options list has *stabilised* (identical across
    `stable_rounds` consecutive 200 ms reads).

    If `check_no_data_warning=True`, also watches for Angular warning messages
    that indicate no data exists for this combination.

    Returns a list of option dicts, or [] if the dropdown is empty / disabled /
    a no-data warning fires.
    """
    end_time = time.time() + timeout
    last_snapshot: list[dict] | None = None
    stable_count = 0

    while time.time() < end_time:
        # ── Check for Angular "no records" warning ──────────────────────────
        if check_no_data_warning:
            try:
                for w in driver.find_elements(By.CSS_SELECTOR, ".missing_field"):
                    if w.is_displayed() and "no state list found" in w.text.lower():
                        return []
            except Exception:
                pass

        # ── Read current options ─────────────────────────────────────────────
        try:
            snapshot = _read_options(driver, select_id)
        except (StaleElementReferenceException, NoSuchElementException):
            last_snapshot = None
            stable_count = 0
            time.sleep(0.2)
            continue

        # ── Stability check ──────────────────────────────────────────────────
        if snapshot and snapshot == last_snapshot:
            stable_count += 1
            if stable_count >= stable_rounds:
                return snapshot
        else:
            last_snapshot = snapshot
            stable_count = 0

        time.sleep(0.2)

    # Timeout: return whatever we last saw (may be [])
    return last_snapshot if last_snapshot else []


def wait_for_dropdown_to_clear(driver, select_id: str, timeout: float = 8) -> bool:
    """
    Waits until the dropdown has 0 meaningful options (i.e. Angular has cleared
    it in preparation for repopulating after a parent change).
    Returns True when cleared, False on timeout.
    """
    end_time = time.time() + timeout
    while time.time() < end_time:
        try:
            opts = _read_options(driver, select_id)
            if not opts:
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def robust_select(
    driver,
    select_id: str,
    value: str,
    downstream_id: str | None = None,
    timeout: float = 15,
) -> bool:
    """
    Selects `value` in `select_id` with full reliability:
      1. Waits for the element to be clickable.
      2. Selects the value.
      3. Verifies the selection actually stuck (Angular can reset it).
      4. If `downstream_id` provided, waits for that dropdown to clear first
         (proving Angular registered the change) before returning.

    Returns True on success, False if selection couldn't be confirmed.
    """
    end_time = time.time() + timeout
    while time.time() < end_time:
        try:
            el = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.ID, select_id))
            )
            Select(el).select_by_value(value)

            # ── Confirm it stuck ────────────────────────────────────────────
            time.sleep(0.15)
            chosen = _get_selected_value(driver, select_id)
            if chosen != value:
                time.sleep(0.3)
                continue  # Angular reset it; retry

            # ── Wait for downstream to clear (proves change was registered) ─
            if downstream_id:
                wait_for_dropdown_to_clear(driver, downstream_id, timeout=5)

            return True

        except (StaleElementReferenceException, ElementNotInteractableException):
            time.sleep(0.3)
        except Exception:
            time.sleep(0.3)

    return False


def wait_for_fresh_table(driver, timeout: float = 35) -> str:
    """
    Waits for the results area to update *after* clicking Submit.
    Strategy: snapshot table row count *before* submit is called (caller
    passes driver immediately after click), then wait for it to change.

    Returns: "ok" | "no_data" | "timeout"
    """
    end_time = time.time() + timeout
    while time.time() < end_time:
        # Check error alert
        try:
            for alert in driver.find_elements(By.CSS_SELECTOR, ".alert-danger-msg"):
                if alert.is_displayed() and "no records found" in alert.text.lower():
                    return "no_data"
        except Exception:
            pass

        # Check populated table
        try:
            rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
            if rows and len(rows) > 0 and rows[0].text.strip():
                return "ok"
        except Exception:
            pass

        time.sleep(0.25)
    return "timeout"


def click_submit_get_result(driver) -> str:
    """Clears previous table, clicks submit, waits for fresh result."""
    # Wipe table body so we don't read stale rows
    try:
        driver.execute_script(
            "const tb = document.querySelector('table tbody'); if (tb) tb.innerHTML = '';"
        )
        # Also remove stale alerts
        driver.execute_script(
            "document.querySelectorAll('.alert-danger-msg').forEach(e => e.remove());"
        )
    except Exception:
        pass

    try:
        btn = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']"))
        )
        driver.execute_script("arguments[0].click();", btn)
    except Exception as e:
        return f"submit_error:{e}"

    return wait_for_fresh_table(driver)


def wait_for_download(directory: str, before: set, timeout: int = 90) -> str | None:
    """
    Waits for a new non-partial file to appear.
    Handles .crdownload partial files and retries until clean.
    """
    end_time = time.time() + timeout
    while time.time() < end_time:
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
    """Hard-reload the page and wait for the first dropdown to be ready."""
    try:
        driver.get(URL)
        WebDriverWait(driver, 30).until(
            lambda d: len(_read_options(d, "m_s_id")) > 0
        )
        return True
    except Exception:
        return False


# =========================================
# COMBO PROCESSOR (single attempt)
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
    """
    Executes one full season/year/commodity/crop/state combination.

    Returns one of:
      "saved"   – file downloaded and renamed
      "skipped" – already in already_done set
      "no_data" – server returned no records
      "timeout" – page failed to respond
      "error:<msg>" – any other exception
    """
    fname = sanitize(
        f"{season['text']}_{year['text']}_{commodity['text']}_{crop['text']}_{state['text']}"
    )
    if fname in already_done:
        return "skipped"

    # ── Season ───────────────────────────────────────────────────────────────
    if not robust_select(driver, "m_s_id", season["value"], downstream_id="m_year"):
        return "error:season select failed"

    # ── Year ─────────────────────────────────────────────────────────────────
    if not robust_select(driver, "m_year", year["value"], downstream_id="comdty_id"):
        return "error:year select failed"

    # ── Commodity ────────────────────────────────────────────────────────────
    if not robust_select(driver, "comdty_id", commodity["value"], downstream_id="c_type_id"):
        return "error:commodity select failed"

    # ── Crop type ────────────────────────────────────────────────────────────
    if not robust_select(driver, "c_type_id", crop["value"], downstream_id="st_id"):
        return "error:crop select failed"

    # ── State ────────────────────────────────────────────────────────────────
    # The state dropdown is the most volatile: it repopulates on every
    # crop-type change.  We must confirm the OPTIONS still include our target
    # value before selecting, because Angular may have re-rendered the list.
    states_now = wait_for_dropdown_options(driver, "st_id", timeout=12, stable_rounds=3)
    if not any(s["value"] == state["value"] for s in states_now):
        return "error:state option disappeared after crop select"

    if not robust_select(driver, "st_id", state["value"]):
        return "error:state select failed"

    # ── Submit ────────────────────────────────────────────────────────────────
    status = click_submit_get_result(driver)
    if status == "no_data":
        return "no_data"
    if status != "ok":
        return f"timeout_or_error:{status}"

    # ── Download ─────────────────────────────────────────────────────────────
    before = set(os.listdir(DOWNLOAD_DIR))
    try:
        xl_btn = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button[aria-label='Download Report in Excel Format']")
            )
        )
        driver.execute_script("arguments[0].click();", xl_btn)
    except Exception as e:
        return f"error:excel button not found: {e}"

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
    log.info("  Procurement Scraper — Hardened Build")
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

                # ── Navigate to this season/year ──────────────────────────────
                # Always reload to get a clean Angular state at the top of
                # each year block — avoids cascading stale-state bugs.
                if not reload_and_wait(driver):
                    log.warning("  Page reload failed, skipping this year block.")
                    total_errors += 1
                    continue

                if not robust_select(driver, "m_s_id", season["value"], downstream_id="m_year"):
                    log.warning("  Season select failed, skipping.")
                    total_errors += 1
                    continue

                if not robust_select(driver, "m_year", year["value"], downstream_id="comdty_id"):
                    log.warning("  Year select failed, skipping.")
                    total_errors += 1
                    continue

                commodities = wait_for_dropdown_options(driver, "comdty_id", timeout=12)
                if not commodities:
                    log.info("  (No commodities available)")
                    continue
                log.info(f"  {len(commodities)} commodities")

                for commodity in commodities:
                    log.info(f"  ┌─ {commodity['text']}")

                    # Re-select season/year/commodity fresh each time to avoid
                    # Angular losing track after prior iterations.
                    if not reload_and_wait(driver):
                        log.warning("  │  Reload failed before commodity. Skipping.")
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

                        # Select crop type; downstream = st_id clears first
                        if not robust_select(driver, "c_type_id", crop["value"], downstream_id="st_id"):
                            log.warning("  │  │  Crop select failed. Skipping.")
                            total_errors += 1
                            continue

                        # Read state list ONCE for this crop (stable)
                        states = wait_for_dropdown_options(
                            driver, "st_id",
                            timeout=12,
                            stable_rounds=3,
                            check_no_data_warning=True,
                        )
                        if not states:
                            log.info("  │  │  └─ (No states for this combination)")
                            total_no_data += 1
                            continue

                        log.info(f"  │  │   {len(states)} states")

                        for state in states:
                            label = f"  │  │  ├─ {state['text']}"

                            # ── Per-combo retry loop ──────────────────────────
                            result = "error:not_attempted"
                            for attempt in range(1, MAX_COMBO_RETRIES + 1):
                                if attempt > 1:
                                    log.info(f"  │  │  │  (retry {attempt}/{MAX_COMBO_RETRIES})")
                                    # On retry: reload and re-navigate to this crop
                                    if not reload_and_wait(driver):
                                        result = "error:reload failed on retry"
                                        break
                                    ok = (
                                        robust_select(driver, "m_s_id", season["value"], downstream_id="m_year")
                                        and robust_select(driver, "m_year", year["value"], downstream_id="comdty_id")
                                        and robust_select(driver, "comdty_id", commodity["value"], downstream_id="c_type_id")
                                        and robust_select(driver, "c_type_id", crop["value"], downstream_id="st_id")
                                    )
                                    if not ok:
                                        result = "error:navigation failed on retry"
                                        break
                                    # Re-read state list after re-navigation
                                    states_retry = wait_for_dropdown_options(
                                        driver, "st_id", timeout=12, stable_rounds=3
                                    )
                                    if not any(s["value"] == state["value"] for s in states_retry):
                                        result = "error:state gone after retry navigation"
                                        break

                                result = process_combination(
                                    driver, season, year, commodity, crop, state, already_done
                                )

                                if result in ("saved", "skipped", "no_data"):
                                    break  # Success — no need to retry
                                if result.startswith("error:state option disappeared"):
                                    break  # State truly not available; no point retrying
                                # Any other error: loop to retry

                            # ── Log outcome ───────────────────────────────────
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
        log.exception(f"[CRITICAL] Unhandled exception: {critical}")

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