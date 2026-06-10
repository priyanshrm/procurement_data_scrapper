import os
import re
import time
import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException

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
options.add_argument("--headless=new")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--disable-gpu")
options.add_argument("--window-size=1920,1080")

prefs = {
    "download.default_directory": DOWNLOAD_DIR,
    "download.prompt_for_download": False,
    "download.directory_upgrade": True,
}
options.add_experimental_option("prefs", prefs)
driver = webdriver.Chrome(options=options)

# =========================================
# DYNAMIC HELPERS
# =========================================
def sanitize(text): return re.sub(r'[\\/*?:"<>|]', "_", text.strip())

def wait_for_page_load(driver):
    WebDriverWait(driver, 30).until(lambda d: d.find_element(By.ID, "m_s_id"))

def select_dropdown_dynamic(driver, parent_id, value, child_id=None):
    parent_el = WebDriverWait(driver, 15).until(EC.element_to_be_clickable((By.ID, parent_id)))
    Select(parent_el).select_by_value(value)
    if child_id:
        try:
            WebDriverWait(driver, 10).until(lambda d: d.find_element(By.ID, child_id))
        except: pass

def click_submit_and_wait(driver):
    submit_btn = WebDriverWait(driver, 15).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']")))
    driver.execute_script("arguments[0].click();", submit_btn)
    print(f"DEBUG: Current URL is {driver.current_url}")
    try:
        WebDriverWait(driver, 60).until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "table tbody tr")) > 0)
        return "ok"
    except: return "timeout"

# =========================================
# MAIN LOOP
# =========================================
try:
    driver.get(URL)
    wait_for_page_load(driver)
    
    seasons = [{"value": "1", "text": "KMS"}, {"value": "2", "text": "RMS"}]
    years = [{"value": "2025-2026", "text": "2025-2026"}, {"value": "2024-2025", "text": "2024-2025"}, 
             {"value": "2023-2024", "text": "2023-2024"}, {"value": "2022-2023", "text": "2022-2023"}, 
             {"value": "2021-2022", "text": "2021-2022"}]

    for season in seasons:
        for year in years:
            select_dropdown_dynamic(driver, "m_s_id", season["value"], "comdty_id")
            select_dropdown_dynamic(driver, "m_year", year["value"], "comdty_id")
            
            commodity_el = driver.find_element(By.ID, "comdty_id")
            for c_val in [o.get_attribute("value") for o in Select(commodity_el).options if o.get_attribute("value") != "0"]:
                select_dropdown_dynamic(driver, "comdty_id", c_val, "c_type_id")
                
                crop_el = driver.find_element(By.ID, "c_type_id")
                for cr_val in [o.get_attribute("value") for o in Select(crop_el).options if o.get_attribute("value") != "0"]:
                    select_dropdown_dynamic(driver, "c_type_id", cr_val, "st_id")
                    
                    state_el = driver.find_element(By.ID, "st_id")
                    for s_val in [o.get_attribute("value") for o in Select(state_el).options if o.get_attribute("value") != "0"]:
                        select_dropdown_dynamic(driver, "st_id", s_val)
                        if click_submit_and_wait(driver) == "ok":
                            btn = driver.find_element(By.CSS_SELECTOR, "button[aria-label='Download Report in Excel Format']")
                            driver.execute_script("arguments[0].click();", btn)
                            time.sleep(5) # Wait for file write
finally:
    driver.quit()