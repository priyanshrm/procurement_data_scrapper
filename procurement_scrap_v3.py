"""
Run this in your GitHub Actions environment to diagnose the page load failure.
Output will tell us exactly what's going wrong.
"""
import os, time, sys
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select

URL = "https://cfpp.nic.in/#/report/3/proc_procuring_agency/"

options = webdriver.ChromeOptions()
options.add_argument("--headless=new")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--window-size=1920,1080")

driver = webdriver.Chrome(options=options)

try:
    print("=== STEP 1: driver.get() ===")
    t0 = time.time()
    driver.get(URL)
    print(f"  Completed in {time.time()-t0:.1f}s")
    print(f"  Title  : {driver.title!r}")
    print(f"  URL    : {driver.current_url!r}")
    print(f"  Source len: {len(driver.page_source)}")

    print("\n=== STEP 2: Snapshot at t=0 ===")
    for sel_id in ("m_s_id", "m_year", "comdty_id", "c_type_id", "st_id"):
        els = driver.find_elements(By.ID, sel_id)
        if els:
            try:
                opts = Select(els[0]).options
                print(f"  {sel_id}: {len(opts)} options, disabled={els[0].get_attribute('disabled')!r}")
            except Exception as e:
                print(f"  {sel_id}: ERROR reading options: {e}")
        else:
            print(f"  {sel_id}: NOT FOUND in DOM")

    print("\n=== STEP 3: Wait 10s for Angular ===")
    time.sleep(10)
    for sel_id in ("m_s_id", "m_year", "comdty_id", "c_type_id", "st_id"):
        els = driver.find_elements(By.ID, sel_id)
        if els:
            try:
                opts = [{"v": o.get_attribute("value"), "t": o.text.strip()} for o in Select(els[0]).options]
                non_empty = [o for o in opts if o["v"] and o["v"] != "0"]
                print(f"  {sel_id}: {len(opts)} opts total, {len(non_empty)} non-placeholder")
                if non_empty:
                    for o in non_empty[:3]:
                        print(f"    {o}")
            except Exception as e:
                print(f"  {sel_id}: ERROR: {e}")
        else:
            print(f"  {sel_id}: NOT IN DOM after 10s wait")

    print("\n=== STEP 4: Browser console errors ===")
    try:
        logs = driver.get_log("browser")
        errors = [e for e in logs if e["level"] in ("SEVERE", "WARNING")]
        if errors:
            for e in errors[-15:]:
                print(f"  [{e['level']}] {e['message'][:250]}")
        else:
            print("  (no errors)")
    except Exception as e:
        print(f"  Could not get logs: {e}")

    print("\n=== STEP 5: All <select> elements in DOM ===")
    selects = driver.find_elements(By.TAG_NAME, "select")
    print(f"  Total <select>: {len(selects)}")
    for s in selects:
        print(f"    id={s.get_attribute('id')!r}  disabled={s.get_attribute('disabled')!r}  opts={len(Select(s).options)}")

    print("\n=== STEP 6: Network — try fetching directly ===")
    import urllib.request
    try:
        req = urllib.request.urlopen("https://cfpp.nic.in/", timeout=10)
        print(f"  cfpp.nic.in root HTTP status: {req.status}")
    except Exception as e:
        print(f"  cfpp.nic.in root unreachable: {e}")

except Exception as e:
    print(f"\n[FATAL] {e}")
    import traceback; traceback.print_exc()
finally:
    driver.quit()
    print("\n=== Done ===")