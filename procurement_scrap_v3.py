import os
import time
import logging
import shutil
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC

# =========================================
# CONFIGURATION
# =========================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

URL = "https://cfpp.nic.in/#/report/3/proc_procuring_agency/"
DOWNLOAD_DIR = os.path.abspath("procurement_data")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# =========================================
# BROWSER SETUP
# =========================================
def get_driver():
    options = webdriver.ChromeOptions()
    prefs = {"download.default_directory": DOWNLOAD_DIR, "download.prompt_for_download": False}
    options.add_experimental_option("prefs", prefs)
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    return webdriver.Chrome(options=options)

def select_dropdown(driver, element_id, value):
    """Helper to wait for and select a dropdown by value."""
    wait = WebDriverWait(driver, 15)
    el = wait.until(EC.element_to_be_clickable((By.ID, element_id)))
    Select(el).select_by_value(value)
    time.sleep(1) # Brief pause to let the site fetch the next dropdown

def get_dropdown_options(driver, element_id):
    """Gets all valid options from a dropdown."""
    try:
        el = WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.ID, element_id)))
        return [{"value": opt.get_attribute("value"), "text": opt.text.strip()} 
                for opt in Select(el).options if opt.get_attribute("value") not in ("0", "")]
    except Exception:
        return []

# =========================================
# MAIN EXECUTION
# =========================================
def main():
    driver = get_driver()
    
    # Define targets (Hardcoded to your specific need to save code)
    seasons = [{"value": "1", "text": "KMS"}]
    years = [{"value": "2021-2022", "text": "2021-2022"}] 
    
    try:
        for season in seasons:
            for year in years:
                log.info(f"Processing: {season['text']} {year['text']}")
                driver.get(URL)
                time.sleep(3)

                select_dropdown(driver, "m_s_id", season["value"])
                select_dropdown(driver, "m_year", year["value"])
                
                commodities = get_dropdown_options(driver, "comdty_id")
                for commodity in commodities:
                    select_dropdown(driver, "comdty_id", commodity["value"])
                    
                    crops = get_dropdown_options(driver, "c_type_id")
                    for crop in crops:
                        select_dropdown(driver, "c_type_id", crop["value"])
                        
                        states = get_dropdown_options(driver, "st_id")
                        for state in states:
                            filename = f"{season['text']}_{year['text']}_{commodity['text']}_{crop['text']}_{state['text']}.xlsx"
                            target_path = os.path.join(DOWNLOAD_DIR, filename.replace("/", "_"))
                            
                            if os.path.exists(target_path):
                                log.info(f"Skipping {state['text']} (Already exists)")
                                continue

                            log.info(f"Fetching: {state['text']}")
                            
                            try:
                                # Start fresh for every state to prevent UI glitches
                                driver.get(URL)
                                time.sleep(2)
                                select_dropdown(driver, "m_s_id", season["value"])
                                select_dropdown(driver, "m_year", year["value"])
                                select_dropdown(driver, "comdty_id", commodity["value"])
                                select_dropdown(driver, "c_type_id", crop["value"])
                                select_dropdown(driver, "st_id", state["value"])
                                
                                # Click Submit
                                submit_btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
                                driver.execute_script("arguments[0].click();", submit_btn)
                                
                                # Check for "No records" or wait for download button
                                time.sleep(5) # Let table load
                                if "no records found" in driver.page_source.lower():
                                    log.info(f"  -> No data for {state['text']}")
                                    continue
                                
                                before_files = set(os.listdir(DOWNLOAD_DIR))
                                dl_btn = WebDriverWait(driver, 20).until(
                                    EC.element_to_be_clickable((By.CSS_SELECTOR, "button[aria-label='Download Report in Excel Format']"))
                                )
                                driver.execute_script("arguments[0].click();", dl_btn)
                                
                                # Wait for download (Max 45 seconds)
                                end_time = time.time() + 45
                                downloaded_file = None
                                while time.time() < end_time:
                                    current_files = set(os.listdir(DOWNLOAD_DIR))
                                    new_files = [f for f in (current_files - before_files) if not f.endswith('.crdownload')]
                                    if new_files:
                                        downloaded_file = new_files[0]
                                        break
                                    time.sleep(1)
                                
                                if downloaded_file:
                                    shutil.move(os.path.join(DOWNLOAD_DIR, downloaded_file), target_path)
                                    log.info(f"  -> Saved {state['text']}")
                                else:
                                    log.warning(f"  -> Timeout downloading {state['text']}")
                                    
                            except Exception as e:
                                log.error(f"  -> Error on {state['text']}: {type(e).__name__}")

    finally:
        driver.quit()
        log.info("Finished.")

if __name__ == "__main__":
    main()