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

EXCLUDE_2026_27 = True
MAX_COMBO_RETRIES = 3
PAGE_LOAD_TIMEOUT = 45   # seconds to wait for page to load
SUBMIT_TIMEOUT    = 45   # seconds to wait for table after submit

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
# LOW-LEVEL DOM HELPERS
# =========================================

def sanitize(text: str) -> str:
    return re.sub(r'[\/*?:"<>|]', "_", text.strip())


def _read_options(driver, select_id: str) -> list[dict]:
    """Return all non-placeholder options from a select. [] if disabled."""
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


def _select_placeholder(driver, select_id: str) -> bool:
    """Force-select the '0' placeholder option to reset a dropdown."""
    try:
        el = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.ID, select_id))
        )
        Select(el).select_by_value("0")
        return True
    except Exception:
        return False

# =========================================
# WAIT PRIMITIVES
# =========================================

def wait_for_dropdown_to_clear(driver, select_id: str, timeout: float = 10) -> bool:
    """Wait until dropdown has zero meaningful (non-placeholder) options."""
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
    Wait until options stabilise across `stable_rounds` consecutive 200ms reads.

    check_no_data_warning: if True, watch for Angular's ".missing_field" warning.
    IMPORTANT: this warning is always present in the DOM (just hidden via CSS).
    We only trust it AFTER the dropdown has had time to populate — specifically,
    only after we've seen at least one non-empty snapshot that then went back to
    empty, OR after a minimum dwell time. Checking it on the very first iteration
    produces false-positives during the brief clear-then-repopulate transition.
    """
    end = time.time() + timeout
    last: list[dict] | None = None
    hits = 0
    EMPTY_DWELL_BEFORE_WARNING = 2.0
    empty_since: float | None = None

    while time.time() < end:
        try:
            snap = _read_options(driver, select_id)
        except (StaleElementReferenceException, NoSuchElementException):
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

            if check_no_data_warning and (time.time() - empty_since) >= EMPTY_DWELL_BEFORE_WARNING:
                try:
                    for w in driver.find_elements(By.CSS_SELECTOR, ".missing_field"):
                        if w.is_displayed() and "no state list found" in w.text.lower():
                            return []
                except Exception:
                    pass

        time.sleep(0.2)

    return last if last else []

# =========================================
# RELOAD + HARD RESET
# =========================================

def reload_and_wait(driver) -> bool:
    """
    Load the page and wait until:
    1. The page title is correct (Angular app loaded).
    2. m_s_id has its 2 static options (KMS/RMS).
    3. Browser storage cleared to avoid Angular form state restoration.
    """
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

        # Clear Angular's persisted form state, then reload once more
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

    except TimeoutException:
        log.warning(f"  reload_and_wait timed out. title={driver.title!r} url={driver.current_url!r}")
        return False
    except Exception as e:
        log.warning(f"  reload_and_wait error: {e}")
        return False

# =========================================
# SELECTION
# =========================================

def robust_select(
    driver,
    select_id: str,
    value: str,
    downstream_id: str | None = None,
    timeout: float = 15,
) -> bool:
    """
    Select `value` in `select_id`, confirm it stuck, then wait for
    `downstream_id` to clear (confirms Angular registered the change event).
    """
    end = time.time() + timeout
    while time.time() < end:
        try:
            el = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((By.ID, select_id))
            )
            Select(el).select_by_value(value)
            time.sleep(0.2)
            if _get_selected_value(driver, select_id) != value:
                time.sleep(0.3)
                continue
            if downstream_id:
                wait_for_dropdown_to_clear(driver, downstream_id, timeout=6)
            return True
        except (StaleElementReferenceException, ElementNotInteractableException):
            time.sleep(0.3)
        except Exception:
            time.sleep(0.3)
    return False


def navigate_to_crop(driver, season, year, commodity, crop) -> bool:
    """Navigate all four dropdowns to a specific crop, with downstream-clear gates."""
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
    try:
        meta_els = driver.find_elements(By.CSS_SELECTOR, ".table-warp .to-bg .col-md-3")
        meta = " | ".join(e.text.strip() for e in meta_els if e.text.strip())
        state_els = driver.find_elements(
            By.CSS_SELECTOR, ".table-warp .row.mb-2 .col-sm-12.col-md-4.col-lg-6"
        )
        state_txt = " | ".join(e.text.strip() for e in state_els if e.text.strip())
        return f"{meta} || {state_txt}"
    except Exception:
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
    """
    Click Submit and wait for a fresh, confirmed result for `expected_state_name`.
    Returns: "ok" | "no_data" | "timeout" | "sig_mismatch"
    """
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
            try:
                sig = _get_result_signature(driver)
                if sig and sig != pre_sig:
                    sig_changed = True
                    log.debug(f"    sig changed → {sig!r}")
                    if expected_state_name.lower() not in sig.lower():
                        log.warning(f"    sig_mismatch: expected {expected_state_name!r} not in {sig!r}")
                        return "sig_mismatch"
            except Exception:
                pass
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
# DOWNLOAD
# =========================================

def wait_for_download(directory: str, before: set, timeout: int = 90) -> str | None:
    end = time.time() + timeout
    while time.time() < end:
        current = set(os.listdir(directory))
        new_files = [
            f for f in (current - before)
            if f.endswith((".xlsx", ".xls", ".csv"))
            and not f.endswith(".crdownload")
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
    """
    Select the state, submit, download the Excel file.

    KEY CHANGE: After a successful download we reload the page immediately
    and re-navigate back to the current crop level. This prevents Angular
    state drift from causing the next state selection to silently fail.
    The caller passes `needs_reload=True` on first use after a prior download.
    """
    fname = sanitize(
        f"{season['text']}_{year['text']}_{commodity['text']}_{crop['text']}_{state['text']}"
    )
    if fname in already_done:
        return "skipped"

    # Confirm state option is still present (Angular may have re-rendered)
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

    # Wait for the table to fully render before triggering the download.
    time.sleep(5)

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
# PER-CROP STATE LOOP  ← NEW helper
# =========================================

def process_states_for_crop(
    driver,
    season: dict,
    year: dict,
    commodity: dict,
    crop: dict,
    already_done: set,
) -> tuple[int, int, int, int]:
    """
    Iterate all states for a given (season, year, commodity, crop).

    After every successful download → reload + re-navigate to this crop
    so the next state selection starts from a clean Angular form.

    Returns (saved, skipped, no_data, errors).
    """
    saved = skipped = no_data = errors = 0

    def _navigate_to_this_crop() -> bool:
        """Reload page and drive dropdowns back to the current crop level."""
        if not reload_and_wait(driver):
            return False
        return navigate_to_crop(driver, season, year, commodity, crop)

    # ── Read initial state list ──────────────────────────────────────────────
    states = wait_for_dropdown_options(
        driver, "st_id",
        timeout=15, stable_rounds=3,
        check_no_data_warning=True,
    )
    if not states:
        log.info("  │  │  └─ (No states)")
        return 0, 0, 1, 0   # count as no_data

    log.info(f"  │  │   {len(states)} states")

    for state in states:
        label = f"  │  │  ├─ {state['text']}"
        result = "error:not_attempted"

        for attempt in range(1, MAX_COMBO_RETRIES + 1):
            if attempt > 1:
                log.info(f"  │  │  │  (retry {attempt}/{MAX_COMBO_RETRIES})")
                # Full reload + re-navigation on retry
                if not _navigate_to_this_crop():
                    result = "error:navigation failed on retry"
                    break
                retry_states = wait_for_dropdown_options(
                    driver, "st_id", timeout=15, stable_rounds=3
                )
                if not any(s["value"] == state["value"] for s in retry_states):
                    result = "error:state gone after retry"
                    break

            result = process_combination(
                driver, season, year, commodity, crop, state, already_done
            )

            if result in ("saved", "skipped", "no_data"):
                break
            if result == "error:state option disappeared":
                break  # genuinely absent

        # ── After a successful download: reload + re-navigate ────────────────
        # This is the core fix: Angular's in-memory state becomes unreliable
        # once a download has been triggered. A fresh page load + full
        # dropdown re-navigation guarantees the next state selection works.
        if result == "saved":
            log.info(f"{label} ... saved")
            saved += 1
            log.info("  │  │  │  (reloading after download…)")
            if not _navigate_to_this_crop():
                log.warning("  │  │  │  Re-navigation after download failed; remaining states may be skipped.")
                errors += 1
                break  # can't continue this crop safely

        elif result == "skipped":
            log.info(f"{label} ... skipped")
            skipped += 1
            # Also reload after a skip — the file already exists, but Angular
            # state may still have drifted from the earlier attempted submit.
            log.info("  │  │  │  (reloading after skip…)")
            if not _navigate_to_this_crop():
                log.warning("  │  │  │  Re-navigation after skip failed.")
                errors += 1
                break

        elif result == "no_data":
            log.info(f"{label} ... no data")
            no_data += 1
            # No file downloaded, but still reload to keep Angular state clean.
            log.info("  │  │  │  (reloading after no_data…)")
            if not _navigate_to_this_crop():
                log.warning("  │  │  │  Re-navigation after no_data failed.")
                errors += 1
                break

        else:
            log.warning(f"{label} ... {result}")
            errors += 1
            # On error also reload so we don't carry forward a broken state.
            log.info("  │  │  │  (reloading after error…)")
            if not _navigate_to_this_crop():
                log.warning("  │  │  │  Re-navigation after error failed.")
                break

    return saved, skipped, no_data, errors


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
    log.info("  Procurement Scraper — v7.0")
    log.info(f"  Destination : {DOWNLOAD_DIR}")
    log.info(f"  Already done: {len(already_done)} files")
    log.info("=" * 60)

    driver = make_driver()

    try:
        log.info("Loading initial page...")
        if not reload_and_wait(driver):
            log.error("Initial page load failed. Check network access to cfpp.nic.in.")
            return
        log.info("Page ready.")

        for season in MARKETING_SEASONS:
            for year in MARKETING_YEARS:
                log.info(f"\n► {season['text']}  {year['text']}")

                if not reload_and_wait(driver):
                    log.warning("  Page reload failed; skipping year block.")
                    total_errors += 1
                    continue

                if not robust_select(driver, "m_s_id", season["value"]):
                    log.warning("  Season select failed; skipping.")
                    total_errors += 1
                    continue

                if not robust_select(driver, "m_year", year["value"], downstream_id="comdty_id"):
                    log.warning("  Year select failed; skipping.")
                    total_errors += 1
                    continue

                commodities = wait_for_dropdown_options(driver, "comdty_id", timeout=15)
                if not commodities:
                    log.info("  (No commodities for this season/year)")
                    continue
                log.info(f"  {len(commodities)} commodities")

                for commodity in commodities:
                    log.info(f"  ┌─ {commodity['text']}")

                    # Fresh page per commodity
                    if not reload_and_wait(driver):
                        log.warning("  │  Reload failed; skipping commodity.")
                        total_errors += 1
                        continue

                    if not robust_select(driver, "m_s_id", season["value"]):
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

                    crop_types = wait_for_dropdown_options(driver, "c_type_id", timeout=12)
                    if not crop_types:
                        log.info("  │  └─ (No crop types)")
                        continue

                    for crop in crop_types:
                        log.info(f"  │  ├─ {crop['text']}")

                        # Select crop type and wait for state dropdown to clear,
                        # then hand off to the dedicated per-crop state loop.
                        if not robust_select(driver, "c_type_id", crop["value"], downstream_id="st_id"):
                            log.warning("  │  │  Crop select failed.")
                            total_errors += 1
                            # Reload and re-navigate back to commodity level
                            if reload_and_wait(driver):
                                robust_select(driver, "m_s_id", season["value"])
                                robust_select(driver, "m_year", year["value"], downstream_id="comdty_id")
                                robust_select(driver, "comdty_id", commodity["value"], downstream_id="c_type_id")
                            continue

                        s, sk, nd, er = process_states_for_crop(
                            driver, season, year, commodity, crop, already_done
                        )
                        total_saved    += s
                        total_skipped  += sk
                        total_no_data  += nd
                        total_errors   += er

                        # After process_states_for_crop the page has been reloaded
                        # and re-navigated to the crop level. To advance to the NEXT
                        # crop we only need to re-select commodity so c_type_id repopulates.
                        # A full reload + season/year/commodity re-nav is safest here.
                        if not reload_and_wait(driver):
                            log.warning("  │  Reload between crops failed.")
                            total_errors += 1
                            break
                        if not robust_select(driver, "m_s_id", season["value"]):
                            log.warning("  │  Season re-select (between crops) failed.")
                            total_errors += 1
                            break
                        if not robust_select(driver, "m_year", year["value"], downstream_id="comdty_id"):
                            log.warning("  │  Year re-select (between crops) failed.")
                            total_errors += 1
                            break
                        if not robust_select(driver, "comdty_id", commodity["value"], downstream_id="c_type_id"):
                            log.warning("  │  Commodity re-select (between crops) failed.")
                            total_errors += 1
                            break

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