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

EXCLUDE_2026_27    = True
MAX_COMBO_RETRIES  = 3
PAGE_LOAD_TIMEOUT  = 45   # seconds to wait for page to load
SUBMIT_TIMEOUT     = 45   # seconds to wait for table after submit

# Restart Chrome proactively after this many downloads to prevent memory crash
DRIVER_RESTART_EVERY = 10

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
    # Memory / stability flags
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
    """Quit driver, swallowing any errors (already dead, etc.)."""
    try:
        driver.quit()
    except Exception:
        pass


def is_driver_alive(driver) -> bool:
    """Check if the ChromeDriver process is still responding."""
    try:
        _ = driver.title   # simple round-trip
        return True
    except Exception:
        return False


def restart_driver(driver) -> webdriver.Chrome:
    """Kill old driver and return a brand-new one."""
    log.info("  ♻  Restarting Chrome driver…")
    safe_quit(driver)
    time.sleep(2)
    new_driver = make_driver()
    log.info("  ♻  Chrome driver restarted.")
    return new_driver

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
        time.sleep(0.15)
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

    except TimeoutException:
        log.warning(f"  reload_and_wait timed out. title={driver.title!r}")
        return False
    except WebDriverException as e:
        log.warning(f"  reload_and_wait WebDriverException: {e}")
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
        except WebDriverException:
            raise   # propagate so callers can detect dead driver
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
# PER-CROP STATE LOOP
# =========================================

def process_states_for_crop(
    driver_ref: list,           # mutable [driver] so we can swap it
    season: dict,
    year: dict,
    commodity: dict,
    crop: dict,
    already_done: set,
    download_counter: list,     # mutable [int] shared across calls
) -> tuple[int, int, int, int]:
    """
    Iterate all states for a given (season, year, commodity, crop).

    driver_ref  : a one-element list holding the current driver so we can
                  replace it in-place when a restart is needed.
    download_counter : a one-element list [int] tracking total downloads
                  across all crops; used to trigger proactive restarts.

    Returns (saved, skipped, no_data, errors).
    """
    saved = skipped = no_data = errors = 0

    def driver():
        return driver_ref[0]

    def _reload_or_restart(reason: str = "") -> bool:
        """
        Try reload_and_wait. If the driver is dead, restart it first.
        Returns True on success.
        """
        if reason:
            log.info(f"  │  │  │  ({reason})")
        if not is_driver_alive(driver()):
            log.warning("  │  │  │  Driver is dead — restarting Chrome…")
            driver_ref[0] = restart_driver(driver())
        return reload_and_wait(driver())

    def _navigate_to_this_crop() -> bool:
        if not _reload_or_restart():
            return False
        return navigate_to_crop(driver(), season, year, commodity, crop)

    def _proactive_restart_if_due() -> bool:
        """
        Restart Chrome every DRIVER_RESTART_EVERY downloads to avoid
        the memory-exhaustion crash we saw at Gujarat.
        Returns True if restart succeeded (or wasn't needed).
        """
        if download_counter[0] > 0 and download_counter[0] % DRIVER_RESTART_EVERY == 0:
            log.info(f"  ♻  Proactive restart after {download_counter[0]} downloads…")
            driver_ref[0] = restart_driver(driver())
            if not reload_and_wait(driver()):
                log.warning("  ♻  Page load after proactive restart failed.")
                return False
            if not navigate_to_crop(driver(), season, year, commodity, crop):
                log.warning("  ♻  Navigation after proactive restart failed.")
                return False
            log.info("  ♻  Proactive restart complete.")
        return True

    # ── Read initial state list ──────────────────────────────────────────────
    states = wait_for_dropdown_options(
        driver(), "st_id",
        timeout=15, stable_rounds=3,
        check_no_data_warning=True,
    )
    if not states:
        log.info("  │  │  └─ (No states)")
        return 0, 0, 1, 0

    log.info(f"  │  │   {len(states)} states")

    for state in states:
        label = f"  │  │  ├─ {state['text']}"
        result = "error:not_attempted"

        for attempt in range(1, MAX_COMBO_RETRIES + 1):
            if attempt > 1:
                log.info(f"  │  │  │  (retry {attempt}/{MAX_COMBO_RETRIES})")
                if not _navigate_to_this_crop():
                    result = "error:navigation failed on retry"
                    break
                retry_states = wait_for_dropdown_options(
                    driver(), "st_id", timeout=15, stable_rounds=3
                )
                if not any(s["value"] == state["value"] for s in retry_states):
                    result = "error:state gone after retry"
                    break

            try:
                result = process_combination(
                    driver(), season, year, commodity, crop, state, already_done
                )
            except WebDriverException as wde:
                log.warning(f"  │  │  WebDriverException during combination: {wde}")
                result = "error:driver_exception"
                # Driver is likely dead; restart immediately
                driver_ref[0] = restart_driver(driver())

            if result in ("saved", "skipped", "no_data"):
                break
            if result == "error:state option disappeared":
                break

        # ── Bookkeeping + reload after every outcome ─────────────────────────
        if result == "saved":
            log.info(f"{label} ... saved")
            saved += 1
            download_counter[0] += 1

            # Proactive restart check
            if not _proactive_restart_if_due():
                errors += 1
                break

            log.info("  │  │  │  (reloading after download…)")
            if not _navigate_to_this_crop():
                log.warning("  │  │  │  Re-navigation after download failed; remaining states may be skipped.")
                errors += 1
                break

        elif result == "skipped":
            log.info(f"{label} ... skipped")
            skipped += 1
            log.info("  │  │  │  (reloading after skip…)")
            if not _navigate_to_this_crop():
                log.warning("  │  │  │  Re-navigation after skip failed.")
                errors += 1
                break

        elif result == "no_data":
            log.info(f"{label} ... no data")
            no_data += 1
            log.info("  │  │  │  (reloading after no_data…)")
            if not _navigate_to_this_crop():
                log.warning("  │  │  │  Re-navigation after no_data failed.")
                errors += 1
                break

        else:
            log.warning(f"{label} ... {result}")
            errors += 1
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


def _reload_with_driver(driver_ref: list) -> bool:
    """
    Wrapper used in main() that auto-restarts the driver if it's dead
    before attempting reload_and_wait.
    """
    if not is_driver_alive(driver_ref[0]):
        log.warning("  Driver dead before reload — restarting…")
        driver_ref[0] = restart_driver(driver_ref[0])
    return reload_and_wait(driver_ref[0])


def main():
    already_done = {
        os.path.splitext(f)[0]
        for f in os.listdir(DOWNLOAD_DIR)
        if f.endswith((".xlsx", ".xls", ".csv"))
    }
    total_saved = total_skipped = total_no_data = total_errors = 0
    download_counter = [0]   # mutable int shared across helpers

    log.info("=" * 60)
    log.info("  Procurement Scraper — v8.0")
    log.info(f"  Destination : {DOWNLOAD_DIR}")
    log.info(f"  Already done: {len(already_done)} files")
    log.info(f"  Driver restart every {DRIVER_RESTART_EVERY} downloads")
    log.info("=" * 60)

    driver_ref = [make_driver()]

    try:
        log.info("Loading initial page...")
        if not reload_and_wait(driver_ref[0]):
            log.error("Initial page load failed. Check network access to cfpp.nic.in.")
            return
        log.info("Page ready.")

        for season in MARKETING_SEASONS:
            for year in MARKETING_YEARS:
                log.info(f"\n► {season['text']}  {year['text']}")

                if not _reload_with_driver(driver_ref):
                    log.warning("  Page reload failed; skipping year block.")
                    total_errors += 1
                    continue

                if not robust_select(driver_ref[0], "m_s_id", season["value"]):
                    log.warning("  Season select failed; skipping.")
                    total_errors += 1
                    continue

                if not robust_select(driver_ref[0], "m_year", year["value"], downstream_id="comdty_id"):
                    log.warning("  Year select failed; skipping.")
                    total_errors += 1
                    continue

                commodities = wait_for_dropdown_options(driver_ref[0], "comdty_id", timeout=15)
                if not commodities:
                    log.info("  (No commodities for this season/year)")
                    continue
                log.info(f"  {len(commodities)} commodities")

                for commodity in commodities:
                    log.info(f"  ┌─ {commodity['text']}")

                    if not _reload_with_driver(driver_ref):
                        log.warning("  │  Reload failed; skipping commodity.")
                        total_errors += 1
                        continue

                    if not robust_select(driver_ref[0], "m_s_id", season["value"]):
                        log.warning("  │  Season re-select failed.")
                        total_errors += 1
                        continue
                    if not robust_select(driver_ref[0], "m_year", year["value"], downstream_id="comdty_id"):
                        log.warning("  │  Year re-select failed.")
                        total_errors += 1
                        continue
                    if not robust_select(driver_ref[0], "comdty_id", commodity["value"], downstream_id="c_type_id"):
                        log.warning("  │  Commodity select failed.")
                        total_errors += 1
                        continue

                    crop_types = wait_for_dropdown_options(driver_ref[0], "c_type_id", timeout=12)
                    if not crop_types:
                        log.info("  │  └─ (No crop types)")
                        continue

                    for crop in crop_types:
                        log.info(f"  │  ├─ {crop['text']}")

                        if not robust_select(driver_ref[0], "c_type_id", crop["value"], downstream_id="st_id"):
                            log.warning("  │  │  Crop select failed.")
                            total_errors += 1
                            if _reload_with_driver(driver_ref):
                                robust_select(driver_ref[0], "m_s_id", season["value"])
                                robust_select(driver_ref[0], "m_year", year["value"], downstream_id="comdty_id")
                                robust_select(driver_ref[0], "comdty_id", commodity["value"], downstream_id="c_type_id")
                            continue

                        s, sk, nd, er = process_states_for_crop(
                            driver_ref, season, year, commodity, crop,
                            already_done, download_counter,
                        )
                        total_saved   += s
                        total_skipped += sk
                        total_no_data += nd
                        total_errors  += er

                        # Between crops: full reload + re-nav to commodity level
                        if not _reload_with_driver(driver_ref):
                            log.warning("  │  Reload between crops failed.")
                            total_errors += 1
                            break
                        if not robust_select(driver_ref[0], "m_s_id", season["value"]):
                            log.warning("  │  Season re-select (between crops) failed.")
                            total_errors += 1
                            break
                        if not robust_select(driver_ref[0], "m_year", year["value"], downstream_id="comdty_id"):
                            log.warning("  │  Year re-select (between crops) failed.")
                            total_errors += 1
                            break
                        if not robust_select(driver_ref[0], "comdty_id", commodity["value"], downstream_id="c_type_id"):
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
            f"  Errors : {total_errors}\n"
            f"  Downloads total: {download_counter[0]}"
        )
        log.info("=" * 60)
        safe_quit(driver_ref[0])


if __name__ == "__main__":
    main()