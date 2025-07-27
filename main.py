# main.py (or app.py) - This will be your FastAPI application

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from pydantic import BaseModel
from typing import List, Optional
import os
import time
import shutil

# Selenium imports
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from webdriver_manager.firefox import GeckoDriverManager
from selenium.webdriver.firefox.service import Service as FirefoxService

# --- Configuration Constants ---
DEFAULT_WEBSITE_URL = "https://safer.fmcsa.dot.gov/CompanySnapshot.aspx"
SELENIUM_WAIT_TIMEOUT = 20 # Default wait time for Selenium elements
TEMP_UPLOAD_DIR = "temp_uploads" # Directory to temporarily store uploaded files

# Ensure temp directory exists
os.makedirs(TEMP_UPLOAD_DIR, exist_ok=True)

app = FastAPI(
    title="NexusFetcher API",
    description="API for scraping data from FMCSA website based on numbers.",
    version="1.0.0"
)

# --- Pydantic Models for Request/Response ---
class ScrapeRequest(BaseModel):
    website_url: str = DEFAULT_WEBSITE_URL
    numbers: List[str] # List of numbers to scrape

class ScrapeResult(BaseModel):
    number_searched: str
    email: str
    name: str
    phone: str

class ScrapeResponse(BaseModel):
    status: str
    message: str
    results: List[ScrapeResult]
    total_processed: int
    errors: List[str] = []

# --- Core Scraping Logic (Adapted from AutomationCore) ---
def _perform_single_scrape(driver, wait, website_url: str, number: str, logs: List[str]):
    """
    Performs the scraping for a single number.
    Returns (email, name, phone) or (N/A, N/A, N/A) on failure.
    Logs are appended to the provided list.
    """
    email, name, phone = "N/A", "N/A", "N/A" # Default values

    try:
        driver.get(website_url)
        logs.append(f"Navigated to: {website_url}")

        radio_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#\\32")))
        radio_button.click()
        logs.append("Clicked the radio button (#\\32).")

        search_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#\\34")))
        search_input.clear()
        search_input.send_keys(number)
        logs.append(f"Entered '{number}' into search input.")

        search_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "body > form > p > table > tbody > tr:nth-child(4) > td > input[type=SUBMIT]")))
        search_button.click()
        logs.append("Clicked Search button.")

        # --- Check for 'No Result Found' or valid SMS result link ---
        try:
            sms_result_link = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "body > p > table > tbody > tr:nth-child(2) > td > table > tbody > tr:nth-child(2) > td > table:nth-child(1) > tbody > tr:nth-child(3) > td > table > tbody > tr:nth-child(2) > td > table > tbody > tr:nth-child(3) > td:nth-child(2) > font > a"))
            )
            sms_result_link.click()
            logs.append("Clicked 'SMS result' link.")

            carrier_details_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#CarrierRegistration > a:nth-child(2)")))
            carrier_details_button.click()
            logs.append("Clicked 'Carrier Details' button.")

            wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "#regBox")))
            logs.append("Carrier details modal/section loaded.")

            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.5) # Small pause to allow content to render after scroll

            try:
                name_element = driver.find_element(By.CSS_SELECTOR, "#regBox > ul.col1 > li:nth-child(1) > span")
                name = name_element.text.strip()
            except NoSuchElementException:
                logs.append("Name element not found.")

            try:
                phone_element = driver.find_element(By.CSS_SELECTOR, "#regBox > ul.col1 > li:nth-child(5) > span")
                phone = phone_element.text.strip()
            except NoSuchElementException:
                logs.append("Phone element not found.")

            try:
                email_element = driver.find_element(By.CSS_SELECTOR, "#regBox > ul.col1 > li:nth-child(7) > span")
                email = email_element.text.strip()
                logs.append(f"Found Email: {email}")
            except NoSuchElementException:
                logs.append("Email element not found.")

            try:
                close_modal_button = wait.until(EC.element_to_be_clickable(
                    (By.XPATH, "//div[@id='CarrierRegistration']//img[@alt='Close'] | //div[@id='CarrierRegistration']//button[contains(.,'X')]")
                ))
                close_modal_button.click()
                logs.append("Closed the carrier details modal.")
            except (TimeoutException, NoSuchElementException):
                logs.append("No explicit close button for modal found or not clickable.")
            
        except TimeoutException:
            logs.append(f"Timeout: No SMS result link found or page did not load within time for number {number}. Skipping details extraction.")
        except NoSuchElementException as e:
            logs.append(f"Element not found: {e} for number {number}. Skipping details extraction.")

    except WebDriverException as e:
        logs.append(f"WebDriver error for number {number}: {e}. Browser might have crashed or disconnected.")
        raise # Re-raise to be caught by the outer try-except
    except Exception as e:
        logs.append(f"An unexpected error occurred for number {number}: {e}")

    return email, name, phone

def _read_numbers_from_file_api(filepath: str, logs: List[str]) -> List[str]:
    """Reads numbers from file, extracting the relevant part, for API context."""
    numbers = []
    try:
        with open(filepath, 'r') as f:
            for line in f:
                stripped_line = line.strip()
                if stripped_line:
                    if stripped_line.startswith("234") and len(stripped_line) >= 10:
                        numbers.append(stripped_line[3:])
                    elif stripped_line.isdigit():
                        numbers.append(stripped_line)
                    else:
                        logs.append(f"Skipping malformed line in file: '{stripped_line}'")
        if not numbers:
            logs.append("No valid numbers found in the file after filtering.")
        return numbers
    except FileNotFoundError:
        logs.append(f"Error: Numbers file not found at '{filepath}'.")
        raise HTTPException(status_code=400, detail=f"Numbers file not found at '{filepath}'.")
    except Exception as e:
        logs.append(f"Error reading numbers file: {e}")
        raise HTTPException(status_code=500, detail=f"Error reading numbers file: {e}")

# --- API Endpoints ---

@app.get("/")
async def root():
    return {"message": "Welcome to NexusFetcher API. Visit /docs for API documentation."}

@app.post("/scrape_by_numbers", response_model=ScrapeResponse)
async def scrape_by_numbers(request: ScrapeRequest):
    """
    Scrapes data for a list of numbers provided in the request body.
    """
    website_url = request.website_url
    numbers_to_scrape = request.numbers
    
    if not numbers_to_scrape:
        raise HTTPException(status_code=400, detail="No numbers provided for scraping.")

    results = []
    errors = []
    logs = []
    processed_count = 0

    driver = None
    try:
        logs.append("Initializing Firefox WebDriver...")
        service = FirefoxService(GeckoDriverManager().install())
        options = webdriver.FirefoxOptions()
        options.add_argument("--headless") # Run headless on the server for efficiency
        driver = webdriver.Firefox(service=service, options=options)
        wait = WebDriverWait(driver, SELENIUM_WAIT_TIMEOUT)
        logs.append("WebDriver initialized.")

        for number in numbers_to_scrape:
            try:
                email, name, phone = _perform_single_scrape(driver, wait, website_url, number, logs)
                results.append(ScrapeResult(number_searched=number, email=email, name=name, phone=phone))
                processed_count += 1
            except Exception as e:
                errors.append(f"Failed to process number {number}: {e}")
                logs.append(f"Error processing {number}: {e}")
                results.append(ScrapeResult(number_searched=number, email="N/A", name="N/A", phone="N/A")) # Add N/A result
                processed_count += 1 # Still count as processed, even if failed

    except WebDriverException as e:
        error_msg = f"Critical WebDriver error during initialization or execution: {e}. Ensure Firefox and GeckoDriver are correctly set up on the server."
        logs.append(error_msg)
        raise HTTPException(status_code=500, detail=error_msg)
    except Exception as e:
        error_msg = f"An unexpected server error occurred: {e}"
        logs.append(error_msg)
        raise HTTPException(status_code=500, detail=error_msg)
    finally:
        if driver:
            try:
                driver.quit()
                logs.append("WebDriver gracefully quit.")
            except WebDriverException:
                logs.append("WebDriver already closed or in an invalid state during quit.")

    return ScrapeResponse(
        status="success" if not errors else "completed_with_errors",
        message="Scraping completed." if not errors else "Scraping completed with some errors. Check 'errors' list.",
        results=results,
        total_processed=processed_count,
        errors=errors
    )

@app.post("/scrape_by_file", response_model=ScrapeResponse)
async def scrape_by_file(
    website_url: str = Form(DEFAULT_WEBSITE_URL),
    numbers_file: UploadFile = File(...)
):
    """
    Scrapes data for numbers provided in an uploaded text file.
    The file should contain one number per line.
    """
    file_path = os.path.join(TEMP_UPLOAD_DIR, numbers_file.filename)
    logs = []
    
    try:
        # Save the uploaded file temporarily
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(numbers_file.file, buffer)
        logs.append(f"Uploaded file saved to {file_path}")

        numbers_to_scrape = _read_numbers_from_file_api(file_path, logs)

        if not numbers_to_scrape:
            raise HTTPException(status_code=400, detail="No valid numbers found in the uploaded file.")

        results = []
        errors = []
        processed_count = 0

        driver = None
        try:
            logs.append("Initializing Firefox WebDriver...")
            service = FirefoxService(GeckoDriverManager().install())
            options = webdriver.FirefoxOptions()
            options.add_argument("--headless") # Run headless on the server
            driver = webdriver.Firefox(service=service, options=options)
            wait = WebDriverWait(driver, SELENIUM_WAIT_TIMEOUT)
            logs.append("WebDriver initialized.")

            for number in numbers_to_scrape:
                try:
                    email, name, phone = _perform_single_scrape(driver, wait, website_url, number, logs)
                    results.append(ScrapeResult(number_searched=number, email=email, name=name, phone=phone))
                    processed_count += 1
                except Exception as e:
                    errors.append(f"Failed to process number {number}: {e}")
                    logs.append(f"Error processing {number}: {e}")
                    results.append(ScrapeResult(number_searched=number, email="N/A", name="N/A", phone="N/A")) # Add N/A result
                    processed_count += 1 # Still count as processed, even if failed

        except WebDriverException as e:
            error_msg = f"Critical WebDriver error during initialization or execution: {e}. Ensure Firefox and GeckoDriver are correctly set up on the server."
            logs.append(error_msg)
            raise HTTPException(status_code=500, detail=error_msg)
        except Exception as e:
            error_msg = f"An unexpected server error occurred: {e}"
            logs.append(error_msg)
            raise HTTPException(status_code=500, detail=error_msg)
        finally:
            if driver:
                try:
                    driver.quit()
                    logs.append("WebDriver gracefully quit.")
                except WebDriverException:
                    logs.append("WebDriver already closed or in an invalid state during quit.")

    except HTTPException:
        raise # Re-raise FastAPI HTTP exceptions
    except Exception as e:
        logs.append(f"Error during file processing: {e}")
        raise HTTPException(status_code=500, detail=f"Error processing file: {e}")
    finally:
        # Clean up the temporary file
        if os.path.exists(file_path):
            os.remove(file_path)
            logs.append(f"Cleaned up temporary file: {file_path}")

    return ScrapeResponse(
        status="success" if not errors else "completed_with_errors",
        message="Scraping completed." if not errors else "Scraping completed with some errors. Check 'errors' list.",
        results=results,
        total_processed=processed_count,
        errors=errors
    )

# --- How to Run This API ---
# 1. Save the code above as, for example, `main.py`.
# 2. Make sure you have `fastapi`, `uvicorn`, `selenium`, `webdriver-manager` installed:
#    `pip install fastapi uvicorn selenium webdriver-manager`
# 3. Run the API from your terminal:
#    `uvicorn main:app --reload`
# 4. Open your browser to `http://127.0.0.1:8000/docs` to see the interactive API documentation.
#    You can test the endpoints directly from there.
